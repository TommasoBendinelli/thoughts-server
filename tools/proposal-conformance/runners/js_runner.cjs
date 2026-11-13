#!/usr/bin/env node
// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
/* Drive the JS parsers on the agent-proposal fixtures.
 *   android-js : Android WebView assets (app/src/main/assets/proposal-review/parser.js);
 *                content-based Markdown dispatch inside parseDocument.
 *   vscode     : VS Code extension parser.js (TeX) + markdownParser.js, dispatched by path
 *                like extension.js documentIsMarkdown().
 * Decisions are emulated like engine.js / extension.js applyDecision: the proposal with the
 * ID must be unique and issue-free; [start,end) is replaced by resolutionText().
 * UTF-16 offsets are converted to code points.
 * Usage: node js_runner.cjs cases.json out.json
 */
const fs = require('node:fs');
const path = require('node:path');
const ANDROID = process.env.ANDROID || path.join(process.env.HOME, 'repos/thoughts-android-integration');
const VSCODE = process.env.VSCODE || path.join(process.env.HOME, 'repos/agentic-tooling-delete-comment/vscode/agent-proposed-edit-review');

const android = require(path.join(ANDROID, 'app/src/main/assets/proposal-review/parser.js'));
const vscodeTex = require(path.join(VSCODE, 'parser.js'));
const vscodeMd = require(path.join(VSCODE, 'markdownParser.js'));

const implementations = {
  'android-js': { parse: (p, text) => android.parseDocument(text), resolve: android.resolutionText },
  vscode: {
    parse: (p, text) => (/\.(?:md|markdown)$/i.test(p) ? vscodeMd.parseMarkdownDocument(text) : vscodeTex.parseDocument(text)),
    resolve: vscodeTex.resolutionText,
  },
};

function run(impl, item) {
  const cp = index => Array.from(item.source.slice(0, index)).length;
  const span = (start, end) => ({ start: cp(start), end: cp(end) });
  let parsed;
  try {
    parsed = impl.parse(item.path, item.source);
  } catch (error) {
    return {
      parse: { proposals: [], issues: [{ message: `throw: ${error.message}` }] },
      decisions: item.decisions.map(() => ({ refused: null, message: 'parse threw' })),
    };
  }
  const proposals = [];
  const issues = [];
  for (const p of parsed.proposals) {
    if (p.issues.length) {
      issues.push({ id: p.id, span: span(p.start, p.end), message: p.issues.join(' | ') });
    } else {
      proposals.push({
        id: p.id, created_at: p.createdAt || '', conversation_id: p.conversationId || '', explanation: p.reason,
        original: p.oldText, replacement: p.newText, span: span(p.start, p.end),
      });
    }
  }
  for (const e of parsed.errors) issues.push({ span: span(e.start, e.end), message: e.message });
  issues.sort((a, b) => a.span.start - b.span.start);
  const decisions = item.decisions.map(d => {
    try {
      const chosen = [...new Set(d.ids)].map(id => {
        const matches = parsed.proposals.filter(p => p.id === id);
        if (matches.length !== 1 || matches[0].issues.length) throw Error(`proposal ${id} missing, duplicated, or malformed`);
        return matches[0];
      }).sort((a, b) => b.start - a.start);
      let text = item.source;
      for (const p of chosen) text = text.slice(0, p.start) + impl.resolve(p, d.action) + text.slice(p.end);
      return { output: text };
    } catch (error) {
      return { refused: null, message: error.message };
    }
  });
  return { parse: { proposals, issues }, decisions };
}

const corpus = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const capabilities = {
  spans: true, issues: true, issue_codes: false, decisions: true, refusal_codes: false,
  fields: ['id', 'created_at', 'conversation_id', 'explanation', 'original', 'replacement'],
};
const out = {};
for (const [name, impl] of Object.entries(implementations)) {
  out[name] = { capabilities, cases: {} };
  for (const item of corpus.cases) out[name].cases[item.id] = run(impl, item);
}
fs.writeFileSync(process.argv[3], JSON.stringify(out, null, 1));
