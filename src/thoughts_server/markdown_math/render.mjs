// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import fs from 'node:fs';
const runtimeVersion = JSON.parse(fs.readFileSync(new URL('./node_modules/@mathjax/src/package.json', import.meta.url), 'utf8')).version;
if (runtimeVersion !== '4.1.3') throw new Error('MathJax 4.1.3 is required');
import {mathjax} from '@mathjax/src/mjs/mathjax.js';
import {TeX} from '@mathjax/src/mjs/input/tex.js';
import {SVG} from '@mathjax/src/mjs/output/svg.js';
import {liteAdaptor} from '@mathjax/src/mjs/adaptors/liteAdaptor.js';
import {RegisterHTMLHandler} from '@mathjax/src/mjs/handlers/html.js';
import '@mathjax/src/mjs/util/asyncLoad/esm.js';
import '@mathjax/src/mjs/input/tex/base/BaseConfiguration.js';
import '@mathjax/src/mjs/input/tex/ams/AmsConfiguration.js';
const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const input = new TeX({packages:['base', 'ams'], formatError(jax,error) { throw error; }});
const output = new SVG({fontCache:'none', linebreaks:{inline:false}});
const document = mathjax.document('', {InputJax:input, OutputJax:output});
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [];
for (const request of requests) {
  const node = await document.convertPromise(request.tex, {display:request.display, em:14, ex:7, containerWidth:430});
  const html = adaptor.outerHTML(node);
  const svg = html.slice(html.indexOf('<svg'), html.lastIndexOf('</svg>')+6);
  if (!svg.startsWith('<svg') || html.includes('data-mml-node="merror"')) throw new Error('Math rendering failed');
  results.push(svg);
}
process.stdout.write(JSON.stringify({version: runtimeVersion, svgs: results}));
