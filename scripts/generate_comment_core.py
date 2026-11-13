#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Generate comment lifecycle services; keep platform code limited to storage/render adapters."""
import argparse
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def outputs():
    rules = json.loads((ROOT / 'protocol/comment_rules.json').read_text())
    assert rules['delete_descendants'] and not rules['location_required'] and rules['preserve_content_on_restore']
    replacements = {
        '@VERSION@': str(rules['version']),
        '@DELETE_REASON@': rules['delete_reason'],
        '@STRIP_CPP@': '\n        '.join(f'payload.remove(QStringLiteral("{field}"));' for field in rules['projection_fields']),
        '@STRIP_KT@': '\n        '.join(f'remove("{field}")' for field in rules['projection_fields']),
    }
    for template, target in [('commentservice.h.in', 'sdk/cpp/include/thoughts/commentservice.h'), ('CommentService.kt.in', 'sdk/kotlin/src/main/kotlin/org/tbend/thoughts/sdk/CommentService.kt')]:
        text = (ROOT / 'scripts/templates' / template).read_text()
        for key, value in replacements.items(): text = text.replace(key, value)
        yield ROOT / target, text

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    for path, text in outputs():
        if args.check:
            if not path.exists() or path.read_text() != text: raise SystemExit(f'Stale comment core: {path}')
        else: path.write_text(text)
