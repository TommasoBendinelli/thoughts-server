// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
// HTML editions: TeX or MathML -> self-contained SVG with the locked MathJax 4.1.3 runtime.
// stdin: [{"kind": "tex"|"mml", "src": "...", "display": bool}]
// stdout: {"version": "4.1.3", "svgs": [svg|null], "errors": [message|null]}
// fontCache 'none' keeps every SVG standalone; glyphs use currentColor (dark mode).
import fs from 'node:fs';
const runtimeVersion = JSON.parse(fs.readFileSync(new URL('./node_modules/@mathjax/src/package.json', import.meta.url), 'utf8')).version;
if (runtimeVersion !== '4.1.3') throw new Error('MathJax 4.1.3 is required');
import {mathjax} from '@mathjax/src/mjs/mathjax.js';
import {TeX} from '@mathjax/src/mjs/input/tex.js';
import {MathML} from '@mathjax/src/mjs/input/mathml.js';
import {SVG} from '@mathjax/src/mjs/output/svg.js';
import {liteAdaptor} from '@mathjax/src/mjs/adaptors/liteAdaptor.js';
import {RegisterHTMLHandler} from '@mathjax/src/mjs/handlers/html.js';
import '@mathjax/src/mjs/util/asyncLoad/esm.js';
import '@mathjax/src/mjs/input/tex/base/BaseConfiguration.js';
import '@mathjax/src/mjs/input/tex/ams/AmsConfiguration.js';
import '@mathjax/src/mjs/input/tex/newcommand/NewcommandConfiguration.js';
import '@mathjax/src/mjs/input/tex/boldsymbol/BoldsymbolConfiguration.js';
import '@mathjax/src/mjs/input/tex/textmacros/TextMacrosConfiguration.js';
const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const output = () => new SVG({fontCache: 'none', linebreaks: {inline: false}});
const tex = new TeX({packages: ['base', 'ams', 'newcommand', 'boldsymbol', 'textmacros'], formatError(jax, error) { throw error; }});
const documents = {
  tex: mathjax.document('', {InputJax: tex, OutputJax: output()}),
  mml: mathjax.document('', {InputJax: new MathML({}), OutputJax: output()}),
};
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const svgs = [], errors = [];
for (const request of requests) {
  try {
    const document = documents[request.kind === 'mml' ? 'mml' : 'tex'];
    const node = await document.convertPromise(request.src, {display: !!request.display, em: 16, ex: 8, containerWidth: 640});
    const html = adaptor.outerHTML(node);
    if (html.includes('data-mml-node="merror"')) {
      throw new Error('merror: ' + ((html.match(/data-mjx-error="([^"]*)"/) || [, ''])[1]));
    }
    const svg = html.slice(html.indexOf('<svg'), html.lastIndexOf('</svg>') + 6);
    if (!svg.startsWith('<svg')) throw new Error('no SVG output');
    svgs.push(svg); errors.push(null);
  } catch (error) {
    svgs.push(null); errors.push(String(error && error.message || error).slice(0, 200));
  }
}
process.stdout.write(JSON.stringify({version: runtimeVersion, svgs, errors}));
