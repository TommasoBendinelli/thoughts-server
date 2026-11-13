# Agent-proposal conformance results

Fixtures: `protocol/fixtures/agent-proposals/cases.json` (140 cases, 8 writer cases).
`pass` = strict (issues and refusal codes exact), `behav` = behavior right but no stable codes/spans, `FAIL` = wrong behavior.

## Totals

| implementation | strict pass | behavioral pass | total |
|---|---|---|---|
| reference | 140 | 140 | 140 |
| server-core | 140 | 140 | 140 |
| server-publisher | 140 | 140 | 140 |
| server-history | 140 | 140 | 140 |
| desktop | 58 | 107 | 140 |
| android-js | 53 | 100 | 140 |
| vscode | 54 | 106 | 140 |

| status | reference | server-core | server-publisher | server-history | desktop | android-js | vscode |
|---|---|---|---|---|---|---|---|
| normative (behavioral) | 132/132 | 132/132 | 132/132 | 132/132 | 99/132 | 95/132 | 99/132 |
| legacy (behavioral) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 5/8 | 7/8 |

## Writers

- md-writer-basic: desktop pass
- md-writer-replacement-without-newline: desktop pass
- md-writer-empty-original: desktop pass
- md-writer-empty-replacement: desktop pass
- md-writer-escapes: desktop pass
- not exercised by any runner: tex-writer-basic, tex-writer-escapes, tex-writer-insertion

## Per case

| case | status | reference | server-core | server-publisher | server-history | desktop | android-js | vscode |
|---|---|---|---|---|---|---|---|---|
| tex-basic-metadata | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-legacy-no-metadata | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-legacy-conversation-in-explanation | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-structured-conversation-wins | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-created-only | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-nested-braces | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-escaped-braces | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-double-backslash-before-brace | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-percent-comment-with-brace | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-escaped-percent | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-double-backslash-percent | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-triple-backslash-percent | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-whitespace-and-comments-between-args | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-space-before-metadata | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-comment-before-metadata | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-real-multiline-arguments | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-metadata-padded | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-id-whitespace | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-id-128-chars | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-id-punctuation | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-insertion | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-deletion | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-two-proposals | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-non-bmp | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-crlf | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-crlf-comment | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-explanation-escaped | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-empty-explanation | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-blank-explanation | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-commented-out | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-escaped-command | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-triple-backslash-command | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-letter-suffix | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-mention | normative | pass | pass | pass | pass | pass | FAIL | FAIL |
| tex-command-definition-braced | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-command-definition-unbraced | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-let-definition | normative | pass | pass | pass | pass | pass | FAIL | FAIL |
| tex-definition-forms | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-real-preamble-definitions | normative | pass | pass | pass | pass | pass | FAIL | FAIL |
| tex-in-verbatim | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-in-doccode | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-in-lstlisting | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-in-minted | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-in-Verbatim-star | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-in-verb | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-after-verbatim | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-verbatim-unclosed | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-commented-verbatim-begin | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-in-linkeddoccode | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-fancyvrb-commandchars-form | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-example-in-markdown-file | normative | pass | pass | pass | pass | behav | FAIL | behav |
| md-real-readme-tex-wrapper | normative | pass | pass | pass | pass | behav | FAIL | behav |
| tex-mentions-markdown-marker | normative | pass | pass | pass | pass | pass | FAIL | pass |
| tex-contains-markdown-block | normative | pass | pass | pass | pass | behav | FAIL | behav |
| tex-uppercase-extension | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-other-extension | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-invalid-id-uppercase | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-invalid-id-empty | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-invalid-id-too-long | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-invalid-id-leading-dash | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-duplicate-id | normative | pass | pass | pass | pass | FAIL | behav | behav |
| tex-duplicate-with-broken | normative | pass | pass | pass | pass | behav | FAIL | FAIL |
| tex-nested | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-agent-remove-inside-argument | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-commented-nested | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-mention-inside-argument | normative | pass | pass | pass | pass | pass | FAIL | FAIL |
| tex-missing-argument | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-only | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-one-bad-one-good | normative | pass | pass | pass | pass | FAIL | behav | behav |
| tex-missing-argument-then-wrapper | normative | pass | pass | pass | pass | FAIL | behav | behav |
| tex-unclosed-brace | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-unclosed-swallows-rest | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-unclosed-comment-at-eof | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-unclosed-bracket | normative | pass | pass | pass | pass | FAIL | behav | behav |
| tex-bad-metadata | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-no-timezone | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-date-only | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-space-after-comma | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-invalid-date | normative | pass | pass | pass | pass | behav | FAIL | FAIL |
| tex-metadata-not-leap-year | normative | pass | pass | pass | pass | behav | FAIL | FAIL |
| tex-metadata-hour-24 | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-metadata-lowercase-z | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-ten-fraction-digits | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-empty-fraction | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-conversation-only | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-uppercase-conversation-metadata | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-metadata-bad-uuid | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-unknown-key | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-empty | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-metadata-offset | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-metadata-six-fraction-digits | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-metadata-nine-fraction-digits | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-metadata-leap-day | normative | pass | pass | pass | pass | pass | pass | pass |
| tex-one-bad-metadata-one-good | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| tex-agent-remove-is-text | normative | pass | pass | pass | pass | behav | behav | behav |
| tex-agent-remove-broken-is-text | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-basic | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-escapes | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-insertion | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-deletion | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-replacement-length | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-multiline | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-created-seven-fraction-digits | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-created-no-fraction | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-created-no-timezone | normative | pass | pass | pass | pass | FAIL | behav | behav |
| md-v2-created-date-only | normative | pass | pass | pass | pass | FAIL | behav | behav |
| md-v2-created-invalid-date | normative | pass | pass | pass | pass | behav | FAIL | FAIL |
| md-v2-created-ten-fraction-digits | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| md-v2-created-offset | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| md-v2-uppercase-conversation | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-blank-explanation | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-unknown-field | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-stripped-trailing-space | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-invalid-id | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-duplicate-id | normative | pass | pass | pass | pass | FAIL | FAIL | behav |
| md-v2-end-at-eof-no-newline | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-not-line-start | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-orphan-end | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v1-orphan-end | legacy | pass | pass | pass | pass | behav | behav | behav |
| md-v2-nested | normative | pass | pass | pass | pass | behav | FAIL | behav |
| md-v2-crlf | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-crlf-content | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-two-proposals | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v2-non-bmp | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-after-tex-example | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-in-fenced-code | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v2-indented-example | normative | pass | pass | pass | pass | FAIL | FAIL | FAIL |
| md-v2-one-bad-one-good | normative | pass | pass | pass | pass | FAIL | behav | FAIL |
| md-v2-missing-end-then-good | normative | pass | pass | pass | pass | FAIL | behav | FAIL |
| md-v2-header-unclosed | normative | pass | pass | pass | pass | FAIL | behav | FAIL |
| md-v2-end-id-mismatch | normative | pass | pass | pass | pass | behav | behav | behav |
| md-v3-marker | normative | pass | pass | pass | pass | FAIL | FAIL | behav |
| md-markdown-extension | normative | pass | pass | pass | pass | pass | pass | pass |
| md-v1-basic | legacy | pass | pass | pass | pass | pass | pass | pass |
| md-v1-extra-field | legacy | pass | pass | pass | pass | pass | pass | pass |
| md-v1-noncanonical-base64 | legacy | pass | pass | pass | pass | behav | FAIL | behav |
| md-v1-invalid-json | legacy | pass | pass | pass | pass | behav | behav | behav |
| md-v1-invalid-date | legacy | pass | pass | pass | pass | behav | FAIL | FAIL |
| md-v1-and-v2 | legacy | pass | pass | pass | pass | pass | pass | pass |
| md-v1-v2-same-id | legacy | pass | pass | pass | pass | behav | FAIL | behav |

## Failure details

- `tex-two-proposals` [desktop] accept['one', 'missing']: refusal code None != unknown_id
- `tex-two-proposals` [android-js] accept['one', 'missing']: refusal code None != unknown_id
- `tex-two-proposals` [vscode] accept['one', 'missing']: refusal code None != unknown_id
- `tex-empty-explanation` [desktop] proposals ['p1'] != expected []
- `tex-empty-explanation` [desktop] issues [] but expected ['invalid_metadata']
- `tex-empty-explanation` [android-js] proposals ['p1'] != expected []
- `tex-empty-explanation` [android-js] issues [] but expected ['invalid_metadata']
- `tex-empty-explanation` [vscode] proposals ['p1'] != expected []
- `tex-empty-explanation` [vscode] issues [] but expected ['invalid_metadata']
- `tex-blank-explanation` [desktop] proposals ['p1'] != expected []
- `tex-blank-explanation` [desktop] issues [] but expected ['invalid_metadata']
- `tex-blank-explanation` [android-js] proposals ['p1'] != expected []
- `tex-blank-explanation` [android-js] issues [] but expected ['invalid_metadata']
- `tex-blank-explanation` [vscode] proposals ['p1'] != expected []
- `tex-blank-explanation` [vscode] issues [] but expected ['invalid_metadata']
- `tex-commented-out` [desktop] accept['agent-c']: refusal code None != unknown_id
- `tex-commented-out` [android-js] accept['agent-c']: refusal code None != unknown_id
- `tex-commented-out` [vscode] accept['agent-c']: refusal code None != unknown_id
- `tex-escaped-command` [desktop] accept['p1']: refusal code None != unknown_id
- `tex-escaped-command` [android-js] accept['p1']: refusal code None != unknown_id
- `tex-escaped-command` [vscode] accept['p1']: refusal code None != unknown_id
- `tex-mention` [android-js] issues ['Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-mention` [vscode] issues ['Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-command-definition-unbraced` [desktop] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-command-definition-unbraced` [android-js] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-command-definition-unbraced` [vscode] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-let-definition` [android-js] issues ['Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-let-definition` [vscode] issues ['Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-definition-forms` [desktop] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-definition-forms` [android-js] issues ['Each \\agentProposedEdit command needs four braced arguments.', 'Proposal metadata requires created=<ISO 8601 timestamp with timezone>.', 'Proposal metadata requires created=<ISO 8601 timestamp with timezone>.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-definition-forms` [vscode] issues ['Each \\agentProposedEdit command needs four braced arguments.', 'Proposal metadata requires created=<ISO 8601 timestamp with timezone>.', 'Proposal metadata requires created=<ISO 8601 timestamp with timezone>.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Each \\agentProposedEdit command needs four braced arguments.', 'Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-real-preamble-definitions` [android-js] issues ['Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-real-preamble-definitions` [vscode] issues ['Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-in-verbatim` [desktop] proposals ['p1'] != expected []
- `tex-in-verbatim` [desktop] accept['p1']: succeeded (expected refusal unknown_id)
- `tex-in-verbatim` [android-js] proposals ['p1'] != expected []
- `tex-in-verbatim` [android-js] accept['p1']: succeeded (expected refusal unknown_id)
- `tex-in-verbatim` [vscode] proposals ['p1'] != expected []
- `tex-in-verbatim` [vscode] accept['p1']: succeeded (expected refusal unknown_id)
- `tex-in-doccode` [desktop] proposals ['p1'] != expected []
- `tex-in-doccode` [android-js] proposals ['p1'] != expected []
- `tex-in-doccode` [vscode] proposals ['p1'] != expected []
- `tex-in-lstlisting` [desktop] proposals ['p1'] != expected []
- `tex-in-lstlisting` [android-js] proposals ['p1'] != expected []
- `tex-in-lstlisting` [vscode] proposals ['p1'] != expected []
- `tex-in-minted` [desktop] proposals ['p1'] != expected []
- `tex-in-minted` [android-js] proposals ['p1'] != expected []
- `tex-in-minted` [vscode] proposals ['p1'] != expected []
- `tex-in-Verbatim-star` [desktop] proposals ['p1'] != expected []
- `tex-in-Verbatim-star` [android-js] proposals ['p1'] != expected []
- `tex-in-Verbatim-star` [vscode] proposals ['p1'] != expected []
- `tex-in-verb` [desktop] issues ['An \\agentProposedEdit stable ID occurs more than once in this file.'] but expected []
- `tex-in-verb` [android-js] issues ['Duplicate proposal ID "p1" in this file.', 'Duplicate proposal ID "p1" in this file.', 'Duplicate proposal ID "p1" in this file.'] but expected []
- `tex-in-verb` [vscode] issues ['Duplicate proposal ID "p1" in this file.', 'Duplicate proposal ID "p1" in this file.', 'Duplicate proposal ID "p1" in this file.'] but expected []
- `tex-after-verbatim` [desktop] proposals ['p1', 'live'] != expected ['live']
- `tex-after-verbatim` [android-js] proposals ['p1', 'live'] != expected ['live']
- `tex-after-verbatim` [vscode] proposals ['p1', 'live'] != expected ['live']
- `tex-verbatim-unclosed` [desktop] proposals ['p1'] != expected []
- `tex-verbatim-unclosed` [android-js] proposals ['p1'] != expected []
- `tex-verbatim-unclosed` [vscode] proposals ['p1'] != expected []
- `tex-fancyvrb-commandchars-form` [desktop] accept['bench-matlab-memory-log-env']: refusal code None != unknown_id
- `tex-fancyvrb-commandchars-form` [android-js] accept['bench-matlab-memory-log-env']: refusal code None != unknown_id
- `tex-fancyvrb-commandchars-form` [vscode] accept['bench-matlab-memory-log-env']: refusal code None != unknown_id
- `tex-example-in-markdown-file` [desktop] accept['p1']: refusal code None != unknown_id
- `tex-example-in-markdown-file` [android-js] proposals ['p1'] != expected []
- `tex-example-in-markdown-file` [android-js] accept['p1']: succeeded (expected refusal unknown_id)
- `tex-example-in-markdown-file` [vscode] accept['p1']: refusal code None != unknown_id
- `md-real-readme-tex-wrapper` [desktop] accept['library-readme-fedora-builder-20260824']: refusal code None != unknown_id
- `md-real-readme-tex-wrapper` [android-js] proposals ['library-readme-fedora-builder-20260824'] != expected []
- `md-real-readme-tex-wrapper` [android-js] accept['library-readme-fedora-builder-20260824']: succeeded (expected refusal unknown_id)
- `md-real-readme-tex-wrapper` [vscode] accept['library-readme-fedora-builder-20260824']: refusal code None != unknown_id
- `tex-mentions-markdown-marker` [android-js] proposals [] != expected ['p1']
- `tex-mentions-markdown-marker` [android-js] issues ['A Markdown proposal marker must begin on its own line.'] but expected []
- `tex-mentions-markdown-marker` [android-js] accept['p1']: refused (proposal p1 missing, duplicated, or malformed)
- `tex-contains-markdown-block` [desktop] accept['md-in-tex']: refusal code None != unknown_id
- `tex-contains-markdown-block` [android-js] proposals ['md-in-tex'] != expected []
- `tex-contains-markdown-block` [android-js] accept['md-in-tex']: succeeded (expected refusal unknown_id)
- `tex-contains-markdown-block` [vscode] accept['md-in-tex']: refusal code None != unknown_id
- `tex-other-extension` [desktop] issues ['Unsupported proposal source format.'] but expected []
- `tex-other-extension` [desktop] accept['p1']: refusal code None != unknown_id
- `tex-other-extension` [android-js] proposals ['p1'] != expected []
- `tex-other-extension` [android-js] accept['p1']: succeeded (expected refusal unknown_id)
- `tex-other-extension` [vscode] proposals ['p1'] != expected []
- `tex-other-extension` [vscode] accept['p1']: succeeded (expected refusal unknown_id)
- `tex-invalid-id-uppercase` [desktop] issues [{}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 123}, 'id': 'Agent-X'}]
- `tex-invalid-id-uppercase` [desktop] accept['Agent-X']: refusal code None != invalid_id
- `tex-invalid-id-uppercase` [android-js] issues [{'span': {'start': 0, 'end': 123}, 'id': 'Agent-X'}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 123}, 'id': 'Agent-X'}]
- `tex-invalid-id-uppercase` [android-js] accept['Agent-X']: refusal code None != invalid_id
- `tex-invalid-id-uppercase` [vscode] issues [{'span': {'start': 0, 'end': 123}, 'id': 'Agent-X'}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 123}, 'id': 'Agent-X'}]
- `tex-invalid-id-uppercase` [vscode] accept['Agent-X']: refusal code None != invalid_id
- `tex-invalid-id-empty` [desktop] issues [{}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 116}, 'id': ''}]
- `tex-invalid-id-empty` [android-js] issues [{'span': {'start': 0, 'end': 116}, 'id': ''}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 116}, 'id': ''}]
- `tex-invalid-id-empty` [vscode] issues [{'span': {'start': 0, 'end': 116}, 'id': ''}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 116}, 'id': ''}]
- `tex-invalid-id-too-long` [desktop] issues [{}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 245}, 'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}]
- `tex-invalid-id-too-long` [android-js] issues [{'span': {'start': 0, 'end': 245}, 'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 245}, 'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}]
- `tex-invalid-id-too-long` [vscode] issues [{'span': {'start': 0, 'end': 245}, 'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 245}, 'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}]
- `tex-invalid-id-leading-dash` [desktop] issues [{}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 119}, 'id': '-p1'}]
- `tex-invalid-id-leading-dash` [android-js] issues [{'span': {'start': 0, 'end': 119}, 'id': '-p1'}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 119}, 'id': '-p1'}]
- `tex-invalid-id-leading-dash` [vscode] issues [{'span': {'start': 0, 'end': 119}, 'id': '-p1'}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 119}, 'id': '-p1'}]
- `tex-duplicate-id` [desktop] proposals [] != expected ['other']
- `tex-duplicate-id` [desktop] accept['other']: refused (An \agentProposedEdit stable ID occurs more than once in this file.)
- `tex-duplicate-id` [desktop] issues [{}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 119}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 120, 'end': 239}, 'id': 'dup'}]
- `tex-duplicate-id` [desktop] accept['dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [desktop] reject['dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [desktop] accept['other', 'dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [android-js] issues [{'span': {'start': 0, 'end': 119}, 'id': 'dup'}, {'span': {'start': 120, 'end': 239}, 'id': 'dup'}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 119}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 120, 'end': 239}, 'id': 'dup'}]
- `tex-duplicate-id` [android-js] accept['dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [android-js] reject['dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [android-js] accept['other', 'dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [vscode] issues [{'span': {'start': 0, 'end': 119}, 'id': 'dup'}, {'span': {'start': 120, 'end': 239}, 'id': 'dup'}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 119}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 120, 'end': 239}, 'id': 'dup'}]
- `tex-duplicate-id` [vscode] accept['dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [vscode] reject['dup']: refusal code None != duplicate_id
- `tex-duplicate-id` [vscode] accept['other', 'dup']: refusal code None != duplicate_id
- `tex-duplicate-with-broken` [desktop] issues [{}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 118}, 'id': 'p1'}, {'code': 'missing_argument', 'span': {'start': 119, 'end': 147}, 'id': 'p1'}]
- `tex-duplicate-with-broken` [desktop] accept['p1']: refusal code None != duplicate_id
- `tex-duplicate-with-broken` [android-js] proposals ['p1'] != expected []
- `tex-duplicate-with-broken` [android-js] accept['p1']: succeeded (expected refusal duplicate_id)
- `tex-duplicate-with-broken` [android-js] issues [{'span': {'start': 119, 'end': 137}}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 118}, 'id': 'p1'}, {'code': 'missing_argument', 'span': {'start': 119, 'end': 147}, 'id': 'p1'}]
- `tex-duplicate-with-broken` [vscode] proposals ['p1'] != expected []
- `tex-duplicate-with-broken` [vscode] accept['p1']: succeeded (expected refusal duplicate_id)
- `tex-duplicate-with-broken` [vscode] issues [{'span': {'start': 119, 'end': 137}}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 118}, 'id': 'p1'}, {'code': 'missing_argument', 'span': {'start': 119, 'end': 147}, 'id': 'p1'}]
- `tex-nested` [desktop] issues [{}] != [{'code': 'nested_proposal', 'span': {'start': 0, 'end': 246}, 'id': 'outer'}]
- `tex-nested` [desktop] accept['outer']: refusal code None != nested_proposal
- `tex-nested` [desktop] accept['inner']: refusal code None != unknown_id
- `tex-nested` [android-js] issues [{'span': {'start': 0, 'end': 246}, 'id': 'outer'}, {'span': {'start': 118, 'end': 240}, 'id': 'inner'}] != [{'code': 'nested_proposal', 'span': {'start': 0, 'end': 246}, 'id': 'outer'}]
- `tex-nested` [android-js] accept['outer']: refusal code None != nested_proposal
- `tex-nested` [android-js] accept['inner']: refusal code None != unknown_id
- `tex-nested` [vscode] issues [{'span': {'start': 0, 'end': 246}, 'id': 'outer'}, {'span': {'start': 118, 'end': 240}, 'id': 'inner'}] != [{'code': 'nested_proposal', 'span': {'start': 0, 'end': 246}, 'id': 'outer'}]
- `tex-nested` [vscode] accept['outer']: refusal code None != nested_proposal
- `tex-nested` [vscode] accept['inner']: refusal code None != unknown_id
- `tex-mention-inside-argument` [android-js] proposals [] != expected ['p1']
- `tex-mention-inside-argument` [android-js] issues ['This proposal contains a malformed nested proposal command.', 'Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-mention-inside-argument` [vscode] proposals [] != expected ['p1']
- `tex-mention-inside-argument` [vscode] issues ['This proposal contains a malformed nested proposal command.', 'Each \\agentProposedEdit command needs four braced arguments.'] but expected []
- `tex-missing-argument` [desktop] issues [{}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 28}, 'id': 'p1'}]
- `tex-missing-argument` [desktop] accept['p1']: refusal code None != missing_argument
- `tex-missing-argument` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 28}, 'id': 'p1'}]
- `tex-missing-argument` [android-js] accept['p1']: refusal code None != missing_argument
- `tex-missing-argument` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 28}, 'id': 'p1'}]
- `tex-missing-argument` [vscode] accept['p1']: refusal code None != missing_argument
- `tex-metadata-only` [desktop] issues [{}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 105}}]
- `tex-metadata-only` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 105}}]
- `tex-metadata-only` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 105}}]
- `tex-one-bad-one-good` [desktop] proposals [] != expected ['good']
- `tex-one-bad-one-good` [desktop] accept['good']: refused (Each \agentProposedEdit command needs four braced arguments.)
- `tex-one-bad-one-good` [desktop] reject['good']: refused (Each \agentProposedEdit command needs four braced arguments.)
- `tex-one-bad-one-good` [desktop] issues [{}] != [{'code': 'missing_argument', 'span': {'start': 121, 'end': 150}, 'id': 'bad'}]
- `tex-one-bad-one-good` [desktop] accept['bad']: refusal code None != missing_argument
- `tex-one-bad-one-good` [desktop] accept['good', 'bad']: refusal code None != missing_argument
- `tex-one-bad-one-good` [android-js] issues [{'span': {'start': 121, 'end': 139}}] != [{'code': 'missing_argument', 'span': {'start': 121, 'end': 150}, 'id': 'bad'}]
- `tex-one-bad-one-good` [android-js] accept['bad']: refusal code None != missing_argument
- `tex-one-bad-one-good` [android-js] accept['good', 'bad']: refusal code None != missing_argument
- `tex-one-bad-one-good` [vscode] issues [{'span': {'start': 121, 'end': 139}}] != [{'code': 'missing_argument', 'span': {'start': 121, 'end': 150}, 'id': 'bad'}]
- `tex-one-bad-one-good` [vscode] accept['bad']: refusal code None != missing_argument
- `tex-one-bad-one-good` [vscode] accept['good', 'bad']: refusal code None != missing_argument
- `tex-missing-argument-then-wrapper` [desktop] proposals [] != expected ['p2']
- `tex-missing-argument-then-wrapper` [desktop] accept['p2']: refused (Each \agentProposedEdit command needs four braced arguments.)
- `tex-missing-argument-then-wrapper` [desktop] issues [{}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 28}, 'id': 'p1'}]
- `tex-missing-argument-then-wrapper` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 28}, 'id': 'p1'}]
- `tex-missing-argument-then-wrapper` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'missing_argument', 'span': {'start': 0, 'end': 28}, 'id': 'p1'}]
- `tex-unclosed-brace` [desktop] issues [{}] != [{'code': 'unclosed_group', 'span': {'start': 0, 'end': 31}, 'id': 'p1'}]
- `tex-unclosed-brace` [desktop] accept['p1']: refusal code None != unclosed_group
- `tex-unclosed-brace` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'unclosed_group', 'span': {'start': 0, 'end': 31}, 'id': 'p1'}]
- `tex-unclosed-brace` [android-js] accept['p1']: refusal code None != unclosed_group
- `tex-unclosed-brace` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'unclosed_group', 'span': {'start': 0, 'end': 31}, 'id': 'p1'}]
- `tex-unclosed-brace` [vscode] accept['p1']: refusal code None != unclosed_group
- `tex-unclosed-swallows-rest` [desktop] proposals [] != expected ['before']
- `tex-unclosed-swallows-rest` [desktop] accept['before']: refused (An \agentProposedEdit argument is missing its closing brace.)
- `tex-unclosed-swallows-rest` [desktop] issues [{}] != [{'code': 'unclosed_group', 'span': {'start': 123, 'end': 280}, 'id': 'broken'}]
- `tex-unclosed-swallows-rest` [desktop] accept['after']: refusal code None != unknown_id
- `tex-unclosed-swallows-rest` [desktop] accept['broken']: refusal code None != unclosed_group
- `tex-unclosed-swallows-rest` [android-js] proposals ['before', 'after'] != expected ['before']
- `tex-unclosed-swallows-rest` [android-js] accept['after']: succeeded (expected refusal unknown_id)
- `tex-unclosed-swallows-rest` [android-js] issues [{'span': {'start': 123, 'end': 141}}] != [{'code': 'unclosed_group', 'span': {'start': 123, 'end': 280}, 'id': 'broken'}]
- `tex-unclosed-swallows-rest` [android-js] accept['broken']: refusal code None != unclosed_group
- `tex-unclosed-swallows-rest` [vscode] proposals ['before', 'after'] != expected ['before']
- `tex-unclosed-swallows-rest` [vscode] accept['after']: succeeded (expected refusal unknown_id)
- `tex-unclosed-swallows-rest` [vscode] issues [{'span': {'start': 123, 'end': 141}}] != [{'code': 'unclosed_group', 'span': {'start': 123, 'end': 280}, 'id': 'broken'}]
- `tex-unclosed-swallows-rest` [vscode] accept['broken']: refusal code None != unclosed_group
- `tex-unclosed-comment-at-eof` [desktop] issues [{}] != [{'code': 'unclosed_group', 'span': {'start': 0, 'end': 65}, 'id': 'p1'}]
- `tex-unclosed-comment-at-eof` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'unclosed_group', 'span': {'start': 0, 'end': 65}, 'id': 'p1'}]
- `tex-unclosed-comment-at-eof` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'unclosed_group', 'span': {'start': 0, 'end': 65}, 'id': 'p1'}]
- `tex-metadata-unclosed-bracket` [desktop] proposals [] != expected ['before']
- `tex-metadata-unclosed-bracket` [desktop] issues [{}] != [{'code': 'unclosed_group', 'span': {'start': 123, 'end': 188}}]
- `tex-metadata-unclosed-bracket` [android-js] issues [{'span': {'start': 123, 'end': 141}}] != [{'code': 'unclosed_group', 'span': {'start': 123, 'end': 188}}]
- `tex-metadata-unclosed-bracket` [vscode] issues [{'span': {'start': 123, 'end': 141}}] != [{'code': 'unclosed_group', 'span': {'start': 123, 'end': 188}}]
- `tex-bad-metadata` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 50}, 'id': 'p1'}]
- `tex-bad-metadata` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-bad-metadata` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 50}, 'id': 'p1'}]
- `tex-bad-metadata` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-bad-metadata` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 50}, 'id': 'p1'}]
- `tex-bad-metadata` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-no-timezone` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 60}, 'id': 'p1'}]
- `tex-metadata-no-timezone` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-no-timezone` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 60}, 'id': 'p1'}]
- `tex-metadata-no-timezone` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-no-timezone` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 60}, 'id': 'p1'}]
- `tex-metadata-no-timezone` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-date-only` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 51}, 'id': 'p1'}]
- `tex-metadata-date-only` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-date-only` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 51}, 'id': 'p1'}]
- `tex-metadata-date-only` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-date-only` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 51}, 'id': 'p1'}]
- `tex-metadata-date-only` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-space-after-comma` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 119}, 'id': 'p1'}]
- `tex-metadata-space-after-comma` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-space-after-comma` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 119}, 'id': 'p1'}]
- `tex-metadata-space-after-comma` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-space-after-comma` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 119}, 'id': 'p1'}]
- `tex-metadata-space-after-comma` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-invalid-date` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 61}, 'id': 'p1'}]
- `tex-metadata-invalid-date` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-invalid-date` [android-js] proposals ['p1'] != expected []
- `tex-metadata-invalid-date` [android-js] issues [] but expected ['invalid_metadata']
- `tex-metadata-invalid-date` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-invalid-date` [vscode] proposals ['p1'] != expected []
- `tex-metadata-invalid-date` [vscode] issues [] but expected ['invalid_metadata']
- `tex-metadata-invalid-date` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-not-leap-year` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 61}, 'id': 'p1'}]
- `tex-metadata-not-leap-year` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-not-leap-year` [android-js] proposals ['p1'] != expected []
- `tex-metadata-not-leap-year` [android-js] issues [] but expected ['invalid_metadata']
- `tex-metadata-not-leap-year` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-not-leap-year` [vscode] proposals ['p1'] != expected []
- `tex-metadata-not-leap-year` [vscode] issues [] but expected ['invalid_metadata']
- `tex-metadata-not-leap-year` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-hour-24` [desktop] proposals ['p1'] != expected []
- `tex-metadata-hour-24` [desktop] issues [] but expected ['invalid_metadata']
- `tex-metadata-hour-24` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-hour-24` [android-js] proposals ['p1'] != expected []
- `tex-metadata-hour-24` [android-js] issues [] but expected ['invalid_metadata']
- `tex-metadata-hour-24` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-hour-24` [vscode] proposals ['p1'] != expected []
- `tex-metadata-hour-24` [vscode] issues [] but expected ['invalid_metadata']
- `tex-metadata-hour-24` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-lowercase-z` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 61}, 'id': 'p1'}]
- `tex-metadata-lowercase-z` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-lowercase-z` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 61}, 'id': 'p1'}]
- `tex-metadata-lowercase-z` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-lowercase-z` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 61}, 'id': 'p1'}]
- `tex-metadata-lowercase-z` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-ten-fraction-digits` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 72}, 'id': 'p1'}]
- `tex-metadata-ten-fraction-digits` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-ten-fraction-digits` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 72}, 'id': 'p1'}]
- `tex-metadata-ten-fraction-digits` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-ten-fraction-digits` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 72}, 'id': 'p1'}]
- `tex-metadata-ten-fraction-digits` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-empty-fraction` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 62}, 'id': 'p1'}]
- `tex-metadata-empty-fraction` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-empty-fraction` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 62}, 'id': 'p1'}]
- `tex-metadata-empty-fraction` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-empty-fraction` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 62}, 'id': 'p1'}]
- `tex-metadata-empty-fraction` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-conversation-only` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 85}, 'id': 'p1'}]
- `tex-metadata-conversation-only` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-conversation-only` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 85}, 'id': 'p1'}]
- `tex-metadata-conversation-only` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-conversation-only` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 85}, 'id': 'p1'}]
- `tex-metadata-conversation-only` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-uppercase-conversation-metadata` [desktop] proposals ['p1'] != expected []
- `tex-uppercase-conversation-metadata` [desktop] issues [] but expected ['invalid_metadata']
- `tex-uppercase-conversation-metadata` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-uppercase-conversation-metadata` [android-js] proposals ['p1'] != expected []
- `tex-uppercase-conversation-metadata` [android-js] issues [] but expected ['invalid_metadata']
- `tex-uppercase-conversation-metadata` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-uppercase-conversation-metadata` [vscode] proposals ['p1'] != expected []
- `tex-uppercase-conversation-metadata` [vscode] issues [] but expected ['invalid_metadata']
- `tex-uppercase-conversation-metadata` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-bad-uuid` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 92}, 'id': 'p1'}]
- `tex-metadata-bad-uuid` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-bad-uuid` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 92}, 'id': 'p1'}]
- `tex-metadata-bad-uuid` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-bad-uuid` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 92}, 'id': 'p1'}]
- `tex-metadata-bad-uuid` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-unknown-key` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 75}, 'id': 'p1'}]
- `tex-metadata-unknown-key` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-unknown-key` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 75}, 'id': 'p1'}]
- `tex-metadata-unknown-key` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-unknown-key` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 75}, 'id': 'p1'}]
- `tex-metadata-unknown-key` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-empty` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 33}, 'id': 'p1'}]
- `tex-metadata-empty` [desktop] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-empty` [android-js] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 33}, 'id': 'p1'}]
- `tex-metadata-empty` [android-js] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-empty` [vscode] issues [{'span': {'start': 0, 'end': 18}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 33}, 'id': 'p1'}]
- `tex-metadata-empty` [vscode] accept['p1']: refusal code None != invalid_metadata
- `tex-metadata-offset` [desktop] proposals ['p1'] != expected []
- `tex-metadata-offset` [desktop] issues [] but expected ['invalid_metadata']
- `tex-metadata-offset` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-offset` [android-js] proposals ['p1'] != expected []
- `tex-metadata-offset` [android-js] issues [] but expected ['invalid_metadata']
- `tex-metadata-offset` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-offset` [vscode] proposals ['p1'] != expected []
- `tex-metadata-offset` [vscode] issues [] but expected ['invalid_metadata']
- `tex-metadata-offset` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `tex-metadata-nine-fraction-digits` [desktop] proposals [] != expected ['p1']
- `tex-metadata-nine-fraction-digits` [desktop] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-metadata-nine-fraction-digits` [android-js] proposals [] != expected ['p1']
- `tex-metadata-nine-fraction-digits` [android-js] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-metadata-nine-fraction-digits` [vscode] proposals [] != expected ['p1']
- `tex-metadata-nine-fraction-digits` [vscode] issues ['Proposal metadata requires created=<ISO 8601 timestamp with timezone>.'] but expected []
- `tex-one-bad-metadata-one-good` [desktop] proposals [] != expected ['good']
- `tex-one-bad-metadata-one-good` [desktop] accept['good']: refused (Proposal metadata requires created=<ISO 8601 timestamp with timezone>.)
- `tex-one-bad-metadata-one-good` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 62}, 'id': 'bad'}]
- `tex-one-bad-metadata-one-good` [android-js] proposals ['bad', 'good'] != expected ['good']
- `tex-one-bad-metadata-one-good` [android-js] issues [] but expected ['invalid_metadata']
- `tex-one-bad-metadata-one-good` [vscode] proposals ['bad', 'good'] != expected ['good']
- `tex-one-bad-metadata-one-good` [vscode] issues [] but expected ['invalid_metadata']
- `tex-agent-remove-is-text` [desktop] accept['agent-remove-0b6c5b7e-1d2a-4c3b-9f8e-112233445566']: refusal code None != unknown_id
- `tex-agent-remove-is-text` [android-js] accept['agent-remove-0b6c5b7e-1d2a-4c3b-9f8e-112233445566']: refusal code None != unknown_id
- `tex-agent-remove-is-text` [vscode] accept['agent-remove-0b6c5b7e-1d2a-4c3b-9f8e-112233445566']: refusal code None != unknown_id
- `md-v2-created-no-timezone` [desktop] proposals ['p1'] != expected []
- `md-v2-created-no-timezone` [desktop] issues [] but expected ['invalid_metadata']
- `md-v2-created-no-timezone` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-no-timezone` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 176, 'end': 206}}, {'span': {'start': 176, 'end': 212}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 219}, 'id': 'p1'}]
- `md-v2-created-no-timezone` [android-js] accept['p1']: refusal code None != invalid_metadata
- `md-v2-created-no-timezone` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 176, 'end': 206}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 219}, 'id': 'p1'}]
- `md-v2-created-no-timezone` [vscode] accept['p1']: refusal code None != invalid_metadata
- `md-v2-created-date-only` [desktop] proposals ['p1'] != expected []
- `md-v2-created-date-only` [desktop] issues [] but expected ['invalid_metadata']
- `md-v2-created-date-only` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-date-only` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 167, 'end': 197}}, {'span': {'start': 167, 'end': 203}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 210}, 'id': 'p1'}]
- `md-v2-created-date-only` [android-js] accept['p1']: refusal code None != invalid_metadata
- `md-v2-created-date-only` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 167, 'end': 197}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 210}, 'id': 'p1'}]
- `md-v2-created-date-only` [vscode] accept['p1']: refusal code None != invalid_metadata
- `md-v2-created-invalid-date` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 220}, 'id': 'p1'}]
- `md-v2-created-invalid-date` [desktop] accept['p1']: refusal code None != invalid_metadata
- `md-v2-created-invalid-date` [android-js] proposals ['p1'] != expected []
- `md-v2-created-invalid-date` [android-js] issues [] but expected ['invalid_metadata']
- `md-v2-created-invalid-date` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-invalid-date` [vscode] proposals ['p1'] != expected []
- `md-v2-created-invalid-date` [vscode] issues [] but expected ['invalid_metadata']
- `md-v2-created-invalid-date` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-ten-fraction-digits` [desktop] proposals ['p1'] != expected []
- `md-v2-created-ten-fraction-digits` [desktop] issues [] but expected ['invalid_metadata']
- `md-v2-created-ten-fraction-digits` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-ten-fraction-digits` [android-js] proposals ['p1'] != expected []
- `md-v2-created-ten-fraction-digits` [android-js] issues [] but expected ['invalid_metadata']
- `md-v2-created-ten-fraction-digits` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-ten-fraction-digits` [vscode] proposals ['p1'] != expected []
- `md-v2-created-ten-fraction-digits` [vscode] issues [] but expected ['invalid_metadata']
- `md-v2-created-ten-fraction-digits` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-offset` [desktop] proposals ['p1'] != expected []
- `md-v2-created-offset` [desktop] issues [] but expected ['invalid_metadata']
- `md-v2-created-offset` [desktop] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-offset` [android-js] proposals ['p1'] != expected []
- `md-v2-created-offset` [android-js] issues [] but expected ['invalid_metadata']
- `md-v2-created-offset` [android-js] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-created-offset` [vscode] proposals ['p1'] != expected []
- `md-v2-created-offset` [vscode] issues [] but expected ['invalid_metadata']
- `md-v2-created-offset` [vscode] accept['p1']: succeeded (expected refusal invalid_metadata)
- `md-v2-uppercase-conversation` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 224}, 'id': 'p1'}]
- `md-v2-uppercase-conversation` [desktop] accept['p1']: refusal code None != invalid_metadata
- `md-v2-uppercase-conversation` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 181, 'end': 211}}, {'span': {'start': 181, 'end': 217}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 224}, 'id': 'p1'}]
- `md-v2-uppercase-conversation` [android-js] accept['p1']: refusal code None != invalid_metadata
- `md-v2-uppercase-conversation` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 181, 'end': 211}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 224}, 'id': 'p1'}]
- `md-v2-uppercase-conversation` [vscode] accept['p1']: refusal code None != invalid_metadata
- `md-v2-blank-explanation` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 226}, 'id': 'p1'}]
- `md-v2-blank-explanation` [desktop] accept['p1']: refusal code None != invalid_metadata
- `md-v2-blank-explanation` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 183, 'end': 213}}, {'span': {'start': 183, 'end': 219}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 226}, 'id': 'p1'}]
- `md-v2-blank-explanation` [android-js] accept['p1']: refusal code None != invalid_metadata
- `md-v2-blank-explanation` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 183, 'end': 213}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 226}, 'id': 'p1'}]
- `md-v2-blank-explanation` [vscode] accept['p1']: refusal code None != invalid_metadata
- `md-v2-unknown-field` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 234}, 'id': 'p1'}]
- `md-v2-unknown-field` [desktop] accept['p1']: refusal code None != invalid_metadata
- `md-v2-unknown-field` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 191, 'end': 221}}, {'span': {'start': 191, 'end': 227}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 234}, 'id': 'p1'}]
- `md-v2-unknown-field` [android-js] accept['p1']: refusal code None != invalid_metadata
- `md-v2-unknown-field` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 191, 'end': 221}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 234}, 'id': 'p1'}]
- `md-v2-unknown-field` [vscode] accept['p1']: refusal code None != invalid_metadata
- `md-v2-stripped-trailing-space` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 237}, 'id': 'p1'}]
- `md-v2-stripped-trailing-space` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 194, 'end': 224}}, {'span': {'start': 194, 'end': 230}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 237}, 'id': 'p1'}]
- `md-v2-stripped-trailing-space` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 194, 'end': 224}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 237}, 'id': 'p1'}]
- `md-v2-invalid-id` [desktop] issues [{}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 234}, 'id': 'Agent-X'}]
- `md-v2-invalid-id` [desktop] accept['Agent-X']: refusal code None != invalid_id
- `md-v2-invalid-id` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 186, 'end': 216}}, {'span': {'start': 186, 'end': 222}}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 234}, 'id': 'Agent-X'}]
- `md-v2-invalid-id` [android-js] accept['Agent-X']: refusal code None != invalid_id
- `md-v2-invalid-id` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 186, 'end': 216}}] != [{'code': 'invalid_id', 'span': {'start': 0, 'end': 234}, 'id': 'Agent-X'}]
- `md-v2-invalid-id` [vscode] accept['Agent-X']: refusal code None != invalid_id
- `md-v2-duplicate-id` [desktop] proposals [] != expected ['other']
- `md-v2-duplicate-id` [desktop] accept['other']: refused (A readable Markdown proposal has invalid or duplicate metadata.)
- `md-v2-duplicate-id` [desktop] issues [{}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 226}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 226, 'end': 452}, 'id': 'dup'}]
- `md-v2-duplicate-id` [desktop] accept['dup']: refusal code None != duplicate_id
- `md-v2-duplicate-id` [android-js] proposals ['dup', 'other'] != expected ['other']
- `md-v2-duplicate-id` [android-js] issues [{'span': {'start': 226, 'end': 452}, 'id': 'dup'}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 226}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 226, 'end': 452}, 'id': 'dup'}]
- `md-v2-duplicate-id` [android-js] accept['dup']: refusal code None != duplicate_id
- `md-v2-duplicate-id` [vscode] issues [{'span': {'start': 0, 'end': 226}, 'id': 'dup'}, {'span': {'start': 226, 'end': 452}, 'id': 'dup'}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 226}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 226, 'end': 452}, 'id': 'dup'}]
- `md-v2-duplicate-id` [vscode] accept['dup']: refusal code None != duplicate_id
- `md-v2-end-at-eof-no-newline` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 179}, 'id': 'p1'}, {'code': 'malformed_marker', 'span': {'start': 181, 'end': 223}}]
- `md-v2-end-at-eof-no-newline` [desktop] accept['p1']: refusal code None != unmatched_marker
- `md-v2-end-at-eof-no-newline` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 181, 'end': 211}}, {'span': {'start': 181, 'end': 217}}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 179}, 'id': 'p1'}, {'code': 'malformed_marker', 'span': {'start': 181, 'end': 223}}]
- `md-v2-end-at-eof-no-newline` [android-js] accept['p1']: refusal code None != unmatched_marker
- `md-v2-end-at-eof-no-newline` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 181, 'end': 211}}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 179}, 'id': 'p1'}, {'code': 'malformed_marker', 'span': {'start': 181, 'end': 223}}]
- `md-v2-end-at-eof-no-newline` [vscode] accept['p1']: refusal code None != unmatched_marker
- `md-v2-not-line-start` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 186, 'end': 229}, 'id': 'p1'}]
- `md-v2-not-line-start` [android-js] issues [{'span': {'start': 5, 'end': 35}}, {'span': {'start': 186, 'end': 216}}, {'span': {'start': 186, 'end': 222}}] != [{'code': 'unmatched_marker', 'span': {'start': 186, 'end': 229}, 'id': 'p1'}]
- `md-v2-not-line-start` [vscode] issues [{'span': {'start': 5, 'end': 35}}, {'span': {'start': 186, 'end': 216}}] != [{'code': 'unmatched_marker', 'span': {'start': 186, 'end': 229}, 'id': 'p1'}]
- `md-v2-orphan-end` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 5, 'end': 53}, 'id': 'agent-x'}]
- `md-v2-orphan-end` [android-js] issues [{'span': {'start': 5, 'end': 35}}, {'span': {'start': 5, 'end': 41}}] != [{'code': 'unmatched_marker', 'span': {'start': 5, 'end': 53}, 'id': 'agent-x'}]
- `md-v2-orphan-end` [vscode] issues [{'span': {'start': 5, 'end': 35}}] != [{'code': 'unmatched_marker', 'span': {'start': 5, 'end': 53}, 'id': 'agent-x'}]
- `md-v1-orphan-end` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 5, 'end': 53}, 'id': 'agent-x'}]
- `md-v1-orphan-end` [android-js] issues [{'span': {'start': 5, 'end': 35}}, {'span': {'start': 5, 'end': 41}}] != [{'code': 'unmatched_marker', 'span': {'start': 5, 'end': 53}, 'id': 'agent-x'}]
- `md-v1-orphan-end` [vscode] issues [{'span': {'start': 5, 'end': 35}}] != [{'code': 'unmatched_marker', 'span': {'start': 5, 'end': 53}, 'id': 'agent-x'}]
- `md-v2-nested` [desktop] issues [{}] != [{'code': 'nested_proposal', 'span': {'start': 0, 'end': 458}, 'id': 'outer'}]
- `md-v2-nested` [desktop] accept['outer']: refusal code None != nested_proposal
- `md-v2-nested` [desktop] accept['inner']: refusal code None != unknown_id
- `md-v2-nested` [android-js] proposals ['inner'] != expected []
- `md-v2-nested` [android-js] accept['inner']: succeeded (expected refusal unknown_id)
- `md-v2-nested` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 366, 'end': 402}}, {'span': {'start': 412, 'end': 442}}] != [{'code': 'nested_proposal', 'span': {'start': 0, 'end': 458}, 'id': 'outer'}]
- `md-v2-nested` [android-js] accept['outer']: refusal code None != nested_proposal
- `md-v2-nested` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 182, 'end': 412}, 'id': 'inner'}, {'span': {'start': 412, 'end': 442}}] != [{'code': 'nested_proposal', 'span': {'start': 0, 'end': 458}, 'id': 'outer'}]
- `md-v2-nested` [vscode] accept['outer']: refusal code None != nested_proposal
- `md-v2-nested` [vscode] accept['inner']: refusal code None != unknown_id
- `md-v2-crlf` [desktop] issues [{}] != [{'code': 'malformed_marker', 'span': {'start': 0, 'end': 39}}, {'code': 'malformed_marker', 'span': {'start': 192, 'end': 236}}]
- `md-v2-crlf` [desktop] accept['p1']: refusal code None != unknown_id
- `md-v2-crlf` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 192, 'end': 222}}, {'span': {'start': 192, 'end': 228}}] != [{'code': 'malformed_marker', 'span': {'start': 0, 'end': 39}}, {'code': 'malformed_marker', 'span': {'start': 192, 'end': 236}}]
- `md-v2-crlf` [android-js] accept['p1']: refusal code None != unknown_id
- `md-v2-crlf` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 192, 'end': 222}}] != [{'code': 'malformed_marker', 'span': {'start': 0, 'end': 39}}, {'code': 'malformed_marker', 'span': {'start': 192, 'end': 236}}]
- `md-v2-crlf` [vscode] accept['p1']: refusal code None != unknown_id
- `md-v2-two-proposals` [desktop] accept['one', 'missing']: refusal code None != unknown_id
- `md-v2-two-proposals` [android-js] accept['one', 'missing']: refusal code None != unknown_id
- `md-v2-two-proposals` [vscode] accept['one', 'missing']: refusal code None != unknown_id
- `md-v2-indented-example` [desktop] issues ['A Markdown proposal marker must begin on its own line.'] but expected []
- `md-v2-indented-example` [desktop] accept['doc-example']: refusal code None != unknown_id
- `md-v2-indented-example` [android-js] issues ['A Markdown proposal marker must begin on its own line.', 'A Markdown proposal marker must begin on its own line.', 'Unmatched Markdown proposal end marker.'] but expected []
- `md-v2-indented-example` [android-js] accept['doc-example']: refusal code None != unknown_id
- `md-v2-indented-example` [vscode] issues ['Markdown proposal marker must begin on its own line.', 'Unknown or unmatched Markdown proposal marker.'] but expected []
- `md-v2-indented-example` [vscode] accept['doc-example']: refusal code None != unknown_id
- `md-v2-one-bad-one-good` [desktop] proposals [] != expected ['good']
- `md-v2-one-bad-one-good` [desktop] accept['good']: refused (A Markdown proposal marker must begin on its own line.)
- `md-v2-one-bad-one-good` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 412, 'end': 456}, 'id': 'bad'}]
- `md-v2-one-bad-one-good` [android-js] issues [{'span': {'start': 183, 'end': 219}}, {'span': {'start': 230, 'end': 260}}, {'span': {'start': 412, 'end': 442}}] != [{'code': 'unmatched_marker', 'span': {'start': 412, 'end': 456}, 'id': 'bad'}]
- `md-v2-one-bad-one-good` [vscode] proposals [] != expected ['good']
- `md-v2-one-bad-one-good` [vscode] accept['good']: refused (proposal good missing, duplicated, or malformed)
- `md-v2-one-bad-one-good` [vscode] issues [{'span': {'start': 0, 'end': 228}, 'id': 'good'}, {'span': {'start': 230, 'end': 260}}, {'span': {'start': 412, 'end': 442}}] != [{'code': 'unmatched_marker', 'span': {'start': 412, 'end': 456}, 'id': 'bad'}]
- `md-v2-missing-end-then-good` [desktop] proposals [] != expected ['good']
- `md-v2-missing-end-then-good` [desktop] accept['good']: refused (A Markdown proposal end marker is missing or mismatched.)
- `md-v2-missing-end-then-good` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 181}, 'id': 'lost'}]
- `md-v2-missing-end-then-good` [desktop] accept['lost']: refusal code None != unmatched_marker
- `md-v2-missing-end-then-good` [android-js] issues [{'span': {'start': 0, 'end': 30}}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 181}, 'id': 'lost'}]
- `md-v2-missing-end-then-good` [android-js] accept['lost']: refusal code None != unmatched_marker
- `md-v2-missing-end-then-good` [vscode] proposals [] != expected ['good']
- `md-v2-missing-end-then-good` [vscode] accept['good']: refused (proposal good missing, duplicated, or malformed)
- `md-v2-missing-end-then-good` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 193, 'end': 421}, 'id': 'good'}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 181}, 'id': 'lost'}]
- `md-v2-missing-end-then-good` [vscode] accept['lost']: refusal code None != unmatched_marker
- `md-v2-header-unclosed` [desktop] proposals [] != expected ['good']
- `md-v2-header-unclosed` [desktop] issues [{}] != [{'code': 'unclosed_group', 'span': {'start': 228, 'end': 322}}]
- `md-v2-header-unclosed` [android-js] issues [{'span': {'start': 228, 'end': 258}}] != [{'code': 'unclosed_group', 'span': {'start': 228, 'end': 322}}]
- `md-v2-header-unclosed` [vscode] proposals [] != expected ['good']
- `md-v2-header-unclosed` [vscode] issues [{'span': {'start': 0, 'end': 228}, 'id': 'good'}, {'span': {'start': 228, 'end': 258}}] != [{'code': 'unclosed_group', 'span': {'start': 228, 'end': 322}}]
- `md-v2-end-id-mismatch` [desktop] issues [{}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 179}, 'id': 'p1'}, {'code': 'unmatched_marker', 'span': {'start': 181, 'end': 227}, 'id': 'other'}]
- `md-v2-end-id-mismatch` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 181, 'end': 211}}, {'span': {'start': 181, 'end': 217}}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 179}, 'id': 'p1'}, {'code': 'unmatched_marker', 'span': {'start': 181, 'end': 227}, 'id': 'other'}]
- `md-v2-end-id-mismatch` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 181, 'end': 211}}] != [{'code': 'unmatched_marker', 'span': {'start': 0, 'end': 179}, 'id': 'p1'}, {'code': 'unmatched_marker', 'span': {'start': 181, 'end': 227}, 'id': 'other'}]
- `md-v3-marker` [desktop] issues [] but expected ['malformed_marker']
- `md-v3-marker` [android-js] issues [] but expected ['malformed_marker']
- `md-v3-marker` [vscode] issues [{'span': {'start': 0, 'end': 30}}] != [{'code': 'malformed_marker', 'span': {'start': 0, 'end': 38}}]
- `md-v1-noncanonical-base64` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 245}}, {'code': 'unmatched_marker', 'span': {'start': 250, 'end': 293}, 'id': 'p1'}]
- `md-v1-noncanonical-base64` [desktop] accept['p1']: refusal code None != unmatched_marker
- `md-v1-noncanonical-base64` [android-js] proposals ['p1'] != expected []
- `md-v1-noncanonical-base64` [android-js] issues [] but expected ['invalid_metadata', 'unmatched_marker']
- `md-v1-noncanonical-base64` [android-js] accept['p1']: succeeded (expected refusal unmatched_marker)
- `md-v1-noncanonical-base64` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 250, 'end': 280}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 245}}, {'code': 'unmatched_marker', 'span': {'start': 250, 'end': 293}, 'id': 'p1'}]
- `md-v1-noncanonical-base64` [vscode] accept['p1']: refusal code None != unmatched_marker
- `md-v1-invalid-json` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 55}}, {'code': 'unmatched_marker', 'span': {'start': 60, 'end': 103}, 'id': 'p1'}]
- `md-v1-invalid-json` [android-js] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 60, 'end': 90}}, {'span': {'start': 60, 'end': 96}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 55}}, {'code': 'unmatched_marker', 'span': {'start': 60, 'end': 103}, 'id': 'p1'}]
- `md-v1-invalid-json` [vscode] issues [{'span': {'start': 0, 'end': 30}}, {'span': {'start': 60, 'end': 90}}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 55}}, {'code': 'unmatched_marker', 'span': {'start': 60, 'end': 103}, 'id': 'p1'}]
- `md-v1-invalid-date` [desktop] issues [{}] != [{'code': 'invalid_metadata', 'span': {'start': 0, 'end': 291}, 'id': 'p1'}]
- `md-v1-invalid-date` [android-js] proposals ['p1'] != expected []
- `md-v1-invalid-date` [android-js] issues [] but expected ['invalid_metadata']
- `md-v1-invalid-date` [vscode] proposals ['p1'] != expected []
- `md-v1-invalid-date` [vscode] issues [] but expected ['invalid_metadata']
- `md-v1-v2-same-id` [desktop] issues [{}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 288}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 288, 'end': 514}, 'id': 'dup'}]
- `md-v1-v2-same-id` [android-js] proposals ['dup'] != expected []
- `md-v1-v2-same-id` [android-js] issues [{'span': {'start': 288, 'end': 514}, 'id': 'dup'}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 288}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 288, 'end': 514}, 'id': 'dup'}]
- `md-v1-v2-same-id` [vscode] issues [{'span': {'start': 0, 'end': 288}, 'id': 'dup'}, {'span': {'start': 288, 'end': 514}, 'id': 'dup'}] != [{'code': 'duplicate_id', 'span': {'start': 0, 'end': 288}, 'id': 'dup'}, {'code': 'duplicate_id', 'span': {'start': 288, 'end': 514}, 'id': 'dup'}]
