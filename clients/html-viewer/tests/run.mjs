// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
// Browser tests of the shared HTML viewer (Playwright, headless).
//
//   node clients/html-viewer/tests/run.mjs [--browser chromium|webkit] [--samples DIR] [--only NAME]
//
// Playwright: `playwright-core` (or `playwright`) importable from here, or
// PLAYWRIGHT_CORE=/path/to/node_modules/playwright-core. Browsers must be installed
// for that Playwright version (npx playwright install chromium webkit).
//
// The edition is served from a fake origin with the desktop's strict CSP
// (script-src 'none'); the viewer files are evaluated into the page as a host does
// with runJavaScript/evaluateJavascript, and the viewer talks to the test through the
// desktop's HTTP transport (POST /<token>/msg). Every request outside the edition is
// recorded and fails the test.
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve, extname } from "node:path";
import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { buildSynthetic } from "./synthetic.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const viewerDir = resolve(here, "..");
const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf("--" + name); return i >= 0 ? args[i + 1] : dflt; };
const browserName = opt("browser", "chromium");
const samplesDir = opt("samples", process.env.THOUGHTS_HTML_SAMPLES ||
  join(process.env.HOME || "", "repos/thoughts-html-spike/latex/sample-editions"));
const only = opt("only", "");

async function loadPlaywright() {
  if (process.env.PLAYWRIGHT_CORE) { return createRequire(join(here, "x.js"))(resolve(process.env.PLAYWRIGHT_CORE)); }
  for (const name of ["playwright-core", "playwright"]) {
    try { return await import(name); } catch (e) { /* next */ }
  }
  throw new Error("Playwright not found: npm install playwright-core, or set PLAYWRIGHT_CORE");
}

const CSP = "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; " +
  "connect-src 'self'; script-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
// HTML attachments run their own scripts in an opaque-origin sandbox without network access
// (so the viewer cannot use the http transport there: the test uses an Android-style bridge).
const ATTACHMENT_CSP = "sandbox allow-scripts; default-src 'self' 'unsafe-inline' data: blob:; connect-src 'none'";
const ORIGIN = "http://edition.test";
const TYPES = { ".html": "text/html; charset=utf-8", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".json": "application/json", ".woff2": "font/woff2" };
const VIEWER_FILES = ["geometry.js", "text.js", "viewer.js"].map((f) => readFileSync(join(viewerDir, f), "utf8"));
const VIEWER_CSS = readFileSync(join(viewerDir, "viewer.css"), "utf8");

let browser;

// Opens an edition: files = {path: Buffer|string} or a directory.
async function openEdition(files, options = {}) {
  const attachment = !!options.attachment;
  const context = await browser.newContext({ viewport: options.viewport || { width: 900, height: 1000 }, deviceScaleFactor: 1,
    colorScheme: options.colorScheme || "light" });
  const page = await context.newPage();
  const messages = [];
  const outside = [];
  const waiters = [];
  const receive = (msg) => {
    messages.push(msg);
    for (const w of waiters.slice()) { if (w.pred(msg)) { waiters.splice(waiters.indexOf(w), 1); w.resolve(msg); } }
  };
  page.on("console", (m) => { if (m.type() === "error") { messages.push({ type: "console-error", text: m.text() }); } });
  page.on("pageerror", (e) => messages.push({ type: "page-error", text: String(e) }));
  if (attachment) { await page.exposeFunction("__tvHostPost", (json) => receive(JSON.parse(json))); }
  await context.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== ORIGIN) { outside.push(req.url()); return route.abort(); }
    if (req.method() === "POST" && url.pathname === "/tok123/msg") {
      receive(JSON.parse(req.postData()));
      return route.fulfill({ status: 204, body: "" });
    }
    const rel = decodeURIComponent(url.pathname.slice(1)) || "index.html";
    let body = null;
    if (typeof files === "string") {
      const p = resolve(files, rel);
      if (p.startsWith(resolve(files)) && existsSync(p)) { body = readFileSync(p); }
    } else if (files[rel] !== undefined) { body = Buffer.from(files[rel]); }
    if (body === null) { return route.fulfill({ status: 404, body: "" }); }
    return route.fulfill({ status: 200, body, headers: { "Content-Type": TYPES[extname(rel)] || "application/octet-stream",
      "Content-Security-Policy": attachment ? ATTACHMENT_CSP : CSP } });
  });
  await page.goto(ORIGIN + "/index.html" + (options.hash || ""));
  if (attachment) {
    // The page's own scripts run, in an opaque origin: inject after load like the apps.
    assert.deepEqual(await page.evaluate(() => [self.origin, typeof window.__page]), ["null", "object"]);
    await page.evaluate(() => { window.ThoughtsViewerBridge = { postMessage: (json) => window.__tvHostPost(json) }; });
  } else {
    // Inline scripts are blocked by the CSP: prove it, then inject like a host.
    const inlineRan = await page.evaluate(() => {
      const s = document.createElement("script");
      s.textContent = "window.__inline = 1";
      document.head.appendChild(s);
      return window.__inline === 1;
    });
    assert.equal(inlineRan, false, "CSP must block inline scripts");
  }
  for (const code of VIEWER_FILES) { await page.evaluate(code); }
  const v = {
    page, messages, outside,
    async call(name, ...callArgs) {
      const raw = await page.evaluate(([n, a]) => window.ThoughtsViewer.call(n, a), [name, JSON.stringify(callArgs)]);
      const res = JSON.parse(raw);
      if (!res.ok) { throw new Error(name + ": " + res.error); }
      return res.result;
    },
    async callRaw(name, ...callArgs) {
      return JSON.parse(await page.evaluate(([n, a]) => window.ThoughtsViewer.call(n, a), [name, JSON.stringify(callArgs)]));
    },
    waitFor(type, pred = () => true, timeout = 5000) {
      const found = messages.find((m) => m.type === type && pred(m) && !m.__seen);
      if (found) { found.__seen = true; return Promise.resolve(found); }
      return new Promise((res, rej) => {
        const w = { pred: (m) => m.type === type && pred(m), resolve: (m) => { clearTimeout(t); m.__seen = true; res(m); } };
        const t = setTimeout(() => { waiters.splice(waiters.indexOf(w), 1); rej(new Error("timeout waiting for " + type)); }, timeout);
        waiters.push(w);
      });
    },
    clear() { messages.forEach((m) => { m.__seen = true; }); },
    async close() { await context.close(); }
  };
  const transport = attachment ? { kind: "android", object: "ThoughtsViewerBridge" } : { kind: "http", url: "/tok123/msg" };
  const loaded = await v.call("load", { transport, css: VIEWER_CSS, theme: options.theme || "light",
    selectionDelayMs: 0, positionThrottleMs: 50, ...(attachment ? { mode: "attachment" } : {}), ...(options.load || {}) });
  assert.equal(loaded.protocol, 1);
  assert.equal(loaded.mode, attachment ? "attachment" : "edition");
  v.ready = await v.waitFor("ready");
  return v;
}

// Answer text fragments (line boxes) and mask blocks, in document coordinates.
async function maskCoverage(v, keys) {
  return v.page.evaluate((keys) => {
    const out = { fragments: [], blocks: [] };
    for (const key of keys) {
      const s = document.querySelector(`[data-tm="start"][data-key="${key}"]`);
      const e = document.querySelector(`[data-tm="end"][data-key="${key}"]`);
      const r = document.createRange();
      r.setStartAfter(s); r.setEndBefore(e);
      const w = document.createTreeWalker(r.commonAncestorContainer.nodeType === 1 ? r.commonAncestorContainer : r.commonAncestorContainer.parentNode, NodeFilter.SHOW_TEXT);
      let n;
      while ((n = w.nextNode())) {
        if (!r.intersectsNode(n) || n.parentElement.closest("svg")) continue;
        const a = n === r.startContainer ? r.startOffset : 0, b = n === r.endContainer ? r.endOffset : n.data.length;
        const re = /\S+/g; let m;
        const text = n.data.slice(a, b);
        while ((m = re.exec(text))) {
          const rr = document.createRange(); rr.setStart(n, a + m.index); rr.setEnd(n, a + m.index + m[0].length);
          for (const q of rr.getClientRects()) {
            const cs = getComputedStyle(n.parentElement);
            if (q.width > 0 && q.height > 0) out.fragments.push({ l: q.left + scrollX, t: q.top + scrollY, r: q.right + scrollX, b: q.bottom + scrollY, word: m[0], clipped: cs.overflowX !== "visible" });
          }
        }
      }
    }
    for (const d of document.querySelectorAll(".tv-mask-block, .tv-mask-frame")) {
      const q = d.getBoundingClientRect();
      out.blocks.push({ l: q.left + scrollX, t: q.top + scrollY, r: q.right + scrollX, b: q.bottom + scrollY });
    }
    return out;
  }, keys);
}

function uncovered(cov) {
  return cov.fragments.filter((f) => !cov.blocks.some((b) => {
    const cx = (f.l + f.r) / 2, cy = (f.t + f.b) / 2;
    return cx >= b.l - 0.5 && cx <= b.r + 0.5 && cy >= b.t - 0.5 && cy <= b.b + 0.5;
  }));
}

// Selects from (word, offset) to the next (word, offset) after it, by text node.
async function selectWords(v, fromWord, fromOffset, toWord, toOffset) {
  return v.page.evaluate(([fw, fo, tw, to]) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n, from = null, toNode = null;
    while ((n = walker.nextNode())) {
      if (!from && n.data === fw) { from = n; if (fw !== tw) continue; }
      if (from && n.data === tw) { toNode = n; break; }
    }
    if (!from || !toNode) throw new Error("no words " + fw + " .. " + tw);
    const r = document.createRange();
    r.setStart(from, fo);
    r.setEnd(toNode, to);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    return String(s);
  }, [fromWord, fromOffset, toWord, toOffset]);
}

async function clickText(v, text, button = "left") {
  const box = await v.page.evaluate((t) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const i = n.data.indexOf(t);
      if (i >= 0) {
        const r = document.createRange(); r.setStart(n, i); r.setEnd(n, i + t.length);
        r.startContainer.parentElement.scrollIntoView({ block: "center" });
        const q = r.getBoundingClientRect();
        return { x: q.left + q.width / 2, y: q.top + q.height / 2 };
      }
    }
    return null;
  }, text);
  assert.ok(box, "text on page: " + text);
  await v.page.mouse.click(box.x, box.y, { button });
  return box;
}

const utf8 = (s) => Buffer.from(s, "utf8");

// ---- tests ------------------------------------------------------------------------

const synthetic = buildSynthetic();
const SYN = { "index.html": synthetic.html, "style.css": synthetic.css };
const CARDS = [
  { id: "c1", anchors: ["review:c1:answer:s1"] },
  { id: "c2", anchors: ["review:c2:answer:s1"] },
  { id: "c3", anchors: ["review:c3:answer:s1"] },
  { id: "c4", anchors: ["review:c4:answer:s1", "review:c4:answer:s2"] },
  { id: "c5", anchors: ["review:c5:answer:s1"] },
  { id: "pending", anchors: [], text_quote: { exact: "common words here", prefix: "number 7 repeats the phrase ", suffix: " and adds" } }
];
const COMMENTS = [
  { id: "p1", anchors: ["comment:p1:s1"] },
  { id: "m1", anchors: ["comment:m1:s1"] },
  { id: "pdfonly", anchors: [], selected_text: "A unique sentence for the selected text comment." },
  { id: "ambiguous", anchors: [], selected_text: "common words here" }
];

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test("synthetic: ready, anchors, no network", async () => {
  const v = await openEdition(SYN);
  try {
    assert.equal(v.ready.source_map, "exact");
    assert.equal(v.ready.anchors.length, 7);
    assert.ok(v.ready.anchors.find((a) => a.key === "comment:p1:s1" && a.kind === "point"));
    assert.deepEqual(v.outside, []);
    const again = JSON.parse(await v.page.evaluate(() => window.ThoughtsViewer.call("nope", "[]")));
    assert.equal(again.ok, false);
  } finally { await v.close(); }
});

test("synthetic: setItems places cards, comments, quotes", async () => {
  const v = await openEdition(SYN);
  try {
    const rep = await v.call("setItems", CARDS, COMMENTS, [], {});
    assert.deepEqual(rep.cards.placed.sort(), ["c1", "c2", "c3", "c4", "pending"]);
    assert.deepEqual(rep.cards.unplaced.map((u) => [u.id, u.reason]), [["c5", "no_anchor_in_html"]]);
    assert.deepEqual(rep.comments.placed.sort(), ["m1", "p1", "pdfonly"]);
    assert.deepEqual(rep.comments.unplaced.map((u) => u.id), ["ambiguous"]);
    const hl = await v.page.evaluate(() => [...CSS.highlights.keys()].sort());
    assert.ok(hl.includes("tv-card") && hl.includes("tv-comment") && hl.includes("tv-pending"), hl.join(","));
    const pending = await v.page.evaluate(() => {
      const h = CSS.highlights.get("tv-pending"); return [...h].map((r) => r.toString());
    });
    assert.deepEqual(pending, ["common words here"]);
    // The pending card's quote picked the occurrence after "number 7".
    const ctx = await v.page.evaluate(() => {
      const r = [...CSS.highlights.get("tv-pending")][0];
      return r.startContainer.parentElement.parentElement.textContent.slice(0, 26);
    });
    assert.equal(ctx, "Filler paragraph number 7 ");
    assert.equal(await v.page.locator("span.tm.tv-point").count(), 1);
  } finally { await v.close(); }
});

test("synthetic: mask block counts, coverage, reveal, inverted", async () => {
  const v = await openEdition(SYN);
  try {
    await v.call("setItems", CARDS, COMMENTS, [], {});
    const expected = { c1: 1, c2: 1, c3: 1, c4: 2 };
    for (const [id, count] of Object.entries(expected)) {
      v.clear();
      const res = await v.call("presentReview", id, {});
      assert.equal(res.blocks, count, `card ${id} blocks`);
      assert.equal(await v.call("reviewMaskBlockCount"), count);
      const m = await v.waitFor("reviewMask");
      assert.equal(m.state, "hidden");
      assert.equal(m.blocks, count);
      const keys = CARDS.find((c) => c.id === id).anchors;
      const cov = await maskCoverage(v, keys);
      assert.deepEqual(uncovered(cov).map((f) => f.word), [], `card ${id}: every answer word is under the mask`);
      // Masked words are invisible (fully contained elements) or under an opaque block.
      const bg = await v.page.evaluate(() => getComputedStyle(document.querySelector(".tv-mask-block")).backgroundColor);
      assert.equal(bg, "rgb(255, 255, 255)");
      assert.equal(await v.call("focusReviewMaskBlock", count - 1), true);
      assert.equal(await v.call("focusReviewMaskBlock", count), false);
    }
    // c1: the paragraphs around the answer stay visible.
    await v.call("presentReview", "c1", {});
    const covered = await v.page.evaluate(() => {
      const blocks = [...document.querySelectorAll(".tv-mask-block")].map((d) => d.getBoundingClientRect());
      const hit = (t) => {
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
        while ((n = w.nextNode())) {
          if (n.data === t) {
            const q = n.parentElement.getBoundingClientRect();
            return blocks.some((b) => q.left + q.width / 2 > b.left && q.left + q.width / 2 < b.right && q.top + q.height / 2 > b.top && q.top + q.height / 2 < b.bottom);
          }
        }
        return null;
      };
      return { before: hit("paragraph."), after: hit("has"), inside: hit("Third") };
    });
    assert.deepEqual(covered, { before: false, after: false, inside: true });
    // Reveal: frames instead of blocks, same count.
    v.clear();
    assert.deepEqual(await v.call("reveal"), { blocks: 1 });
    assert.equal(await v.page.locator(".tv-mask-block").count(), 0);
    assert.equal(await v.page.locator(".tv-mask-frame").count(), 1);
    assert.equal((await v.waitFor("reviewMask")).state, "revealed");
    assert.equal(await v.page.locator(".tv-masked").count(), 0);
    // Inverted: a document-sized cover with the answer cut out.
    await v.call("presentReview", "c1", { inverted: true });
    const path = await v.page.evaluate(() => document.querySelector(".tv-mask-inverted path").getAttribute("d"));
    assert.equal((path.match(/M/g) || []).length, 2);
    assert.equal(await v.call("setReviewInverted", false), true);
    assert.equal(await v.page.locator(".tv-mask-block").count(), 1);
    await v.call("clearReview");
    assert.equal(await v.call("reviewMaskBlockCount"), 0);
    assert.equal(await v.page.locator(".tv-mask-block, .tv-mask-frame, .tv-mask-inverted").count(), 0);
  } finally { await v.close(); }
});

test("synthetic: masked text cannot be selected or copied", async () => {
  const v = await openEdition(SYN);
  try {
    await v.call("setItems", CARDS, COMMENTS, [], {});
    await v.call("presentReview", "c1", {});
    // A selection from p1 into the hidden answer stops at the answer.
    await selectWords(v, "lazy", 0, "Third", 3);
    await v.page.waitForTimeout(50);
    const copied = await v.call("copySelection");
    assert.ok(copied.startsWith("lazy dog."), copied);
    assert.ok(!copied.includes("Second") && !copied.includes("Thi"), copied);
    // Dragging with the mouse inside the hidden answer selects nothing.
    await v.call("clearSelection");
    const block = await v.page.evaluate(() => { const q = document.querySelector(".tv-mask-block").getBoundingClientRect(); return [q.left, q.top, q.right, q.bottom]; });
    await v.page.mouse.move(block[0] + 10, block[1] + 5);
    await v.page.mouse.down();
    await v.page.mouse.move(block[2] - 10, block[3] - 5, { steps: 5 });
    await v.page.mouse.up();
    assert.equal(await v.call("copySelection"), "");
    // After reveal the answer is selectable again.
    await v.call("reveal");
    await selectWords(v, "lazy", 0, "Third", 3);
    assert.ok((await v.call("copySelection")).includes("Second paragraph"));
  } finally { await v.close(); }
});

test("synthetic: selection → text_quote and exact source_range", async () => {
  const v = await openEdition(SYN);
  try {
    v.clear();
    const text = await selectWords(v, "quick", 2, "jumps", 3);
    assert.equal(text, "ick brown fox jum");
    const sel = await v.waitFor("selection", (m) => !m.empty);
    assert.equal(sel.text_quote.exact, "ick brown fox jum");
    assert.equal(sel.text_quote.prefix, "Notes on naïve testing The qu");
    assert.ok(sel.text_quote.suffix.startsWith("ps over the lazy dog."));
    assert.equal(Array.from(sel.text_quote.suffix).length, 64);
    const sr = sel.source_range;
    assert.equal(sr.source_file, "note.md");
    assert.equal(sr.exact, true);
    assert.equal(utf8(synthetic.source).subarray(sr.start_byte, sr.end_byte).toString("utf8"), "ick brown fox jum");
    assert.ok(sel.rects.length >= 1 && sel.dpr === 1);
    // Multi-byte characters: byte offsets, not UTF-16 offsets.
    await selectWords(v, "naïve", 1, "testing", 4);
    const sel2 = await v.call("getSelection");
    assert.equal(utf8(synthetic.source).subarray(sel2.source_range.start_byte, sel2.source_range.end_byte).toString("utf8"), "aïve test");
    // A word whose HTML text differs from its source (smart quotes): whole-word edge, not exact.
    await selectWords(v, "“quoted”", 2, "follows", 3);
    const sel3 = await v.call("getSelection");
    assert.equal(sel3.source_range.exact, false);
    assert.equal(utf8(synthetic.source).subarray(sel3.source_range.start_byte, sel3.source_range.end_byte).toString("utf8"), '"quoted" word fol');
    // Collapsing the selection reports an empty selection.
    await v.call("clearSelection");
    await v.waitFor("selection", (m) => m.empty === true);
  } finally { await v.close(); }
});

test("synthetic: answer quote for feedback, feedback highlights after reveal", async () => {
  const v = await openEdition(SYN);
  try {
    const fb = [{ id: "f1", card_id: "c1", polarity: "positive", text_quote: { exact: "another sentence", prefix: "answer after ", suffix: " of" } },
      { id: "f2", card_id: "c1", polarity: "negative", text_quote: { exact: "not in the answer" } }];
    const rep = await v.call("setItems", CARDS, COMMENTS, fb, {});
    assert.deepEqual(rep.feedback.placed, ["f1"]);
    assert.deepEqual(rep.feedback.unplaced.map((u) => [u.id, u.reason]), [["f2", "orphaned"]]);
    await v.call("presentReview", "c1", {});
    assert.equal(await v.page.evaluate(() => CSS.highlights.has("tv-feedback-positive")), false);
    await v.call("reveal");
    assert.equal(await v.page.evaluate(() => [...CSS.highlights.get("tv-feedback-positive")][0].toString()), "another sentence");
    await selectWords(v, "Third", 0, "paragraph", 9);
    const sel = await v.call("getSelection");
    assert.equal(sel.answer.card_id, "c1");
    assert.equal(sel.answer.text_quote.exact, "Third paragraph");
    // Context comes from the answer only (both paragraphs of c1), 32 code points like the desktop.
    assert.equal(sel.answer.text_quote.prefix, "e reading column of the viewer. ");
    assert.equal(Array.from(sel.answer.text_quote.suffix).length, 32);
  } finally { await v.close(); }
});

test("synthetic: selection starting at the answer's first character gets the answer quote", async () => {
  const v = await openEdition(SYN);
  try {
    await v.call("setItems", CARDS, COMMENTS, [], {});
    await v.call("presentReview", "c1", {});
    await v.call("reveal");
    // The collapsed space before the answer's first word must not count as part of the selection.
    await selectWords(v, "Second", 0, "paragraph", 9);
    let sel = await v.call("getSelection");
    assert.equal(sel.text_quote.exact, "Second paragraph");
    assert.ok(sel.answer, "answer payload for a selection at the answer start");
    assert.equal(sel.answer.card_id, "c1");
    assert.equal(sel.answer.text_quote.exact, "Second paragraph");
    // A selection of the whole answer, first to last character.
    await selectWords(v, "Second", 0, "length.", 7);
    sel = await v.call("getSelection");
    assert.ok(sel.answer, "answer payload for the whole answer");
    assert.ok(sel.answer.text_quote.exact.startsWith("Second paragraph starts"));
    assert.ok(sel.answer.text_quote.exact.endsWith("reasonable length."));
    // One character before the answer is outside it.
    await selectWords(v, "The", 0, "Second", 6);
    sel = await v.call("getSelection");
    assert.equal(sel.answer, undefined);
  } finally { await v.close(); }
});

test("synthetic: forwardKeys sends the app's shortcuts, never a text field's keys", async () => {
  const v = await openEdition(SYN, { load: { forwardKeys: ["s", "Escape", "Meta+Shift+u", "Ctrl+Shift+u"] } });
  try {
    const keys = () => v.messages.filter((m) => m.type === "key");
    await v.page.evaluate(() => {
      window.__seen = [];
      window.addEventListener("keydown", (e) => { if (!["Shift", "Meta", "Control", "Alt"].includes(e.key)) window.__seen.push(e.key); });
      document.body.focus();
    });
    await v.page.keyboard.press("s");
    await v.waitFor("key", (m) => m.key === "s");
    let k = keys().at(-1);
    assert.deepEqual([k.forwarded, k.meta, k.ctrl, k.alt, k.shift, k.repeat], [true, false, false, false, false, false]);
    await v.page.keyboard.press("Escape");
    assert.equal((await v.waitFor("key", (m) => m.key === "Escape")).forwarded, true);
    await v.page.keyboard.press("Meta+Shift+KeyU");
    k = await v.waitFor("key", (m) => m.code === "KeyU");
    assert.deepEqual([k.forwarded, k.meta, k.shift], [true, true, true]);
    await v.page.keyboard.press("Control+Shift+KeyU");
    k = await v.waitFor("key", (m) => m.code === "KeyU" && m.ctrl);
    assert.equal(k.forwarded, true);
    // Not listed: Shift+S, plain d. Not forwarded (forwardModifierKeys is off) and left to the page.
    const before = keys().length;
    await v.page.keyboard.press("Shift+KeyS");
    await v.page.keyboard.press("d");
    // A forwarded key is cancelled in the page: the page's own listeners never see it.
    assert.deepEqual(await v.page.evaluate(() => window.__seen), ["S", "d"]);
    // In a text field the key is typed, not forwarded.
    await v.page.evaluate(() => {
      const input = document.createElement("input");
      input.id = "tv-test-input";
      document.body.appendChild(input);
      input.focus();
    });
    await v.page.keyboard.press("s");
    await v.page.keyboard.press("Escape");
    await v.page.waitForTimeout(150);
    assert.equal(await v.page.evaluate(() => document.getElementById("tv-test-input").value), "s");
    assert.equal(keys().length, before);
  } finally { await v.close(); }
});

test("synthetic: tap on highlight, context menu", async () => {
  const v = await openEdition(SYN);
  try {
    await v.call("setItems", CARDS, COMMENTS, [], {});
    v.clear();
    await clickText(v, "highlighted");
    const tap = await v.waitFor("itemTapped");
    assert.deepEqual([tap.id, tap.kind], ["m1", "comment"]);
    await clickText(v, "short");
    assert.deepEqual([(await v.waitFor("itemTapped")).id], ["c3"]);
    await v.page.click("span.tm.tv-point");
    assert.deepEqual([(await v.waitFor("itemTapped")).id], ["p1"]);
    await selectWords(v, "Fourth", 0, "paragraph", 9);
    await clickText(v, "Fourth", "right");
    const menu = await v.waitFor("contextmenu");
    assert.equal(menu.selection.text_quote.exact, "Fourth paragraph");
    assert.equal(menu.point.length, 2);
  } finally { await v.close(); }
});

test("synthetic: links are intercepted", async () => {
  const v = await openEdition(SYN);
  try {
    const url = v.page.url();
    v.clear();
    await clickText(v, "PDF");
    const l1 = await v.waitFor("link");
    assert.equal(l1.kind, "thoughts");
    assert.deepEqual(l1.link, { link_kind: "pdf", params: { key: "other_doc", anchor: "document-start" } });
    await clickText(v, "source");
    const l2 = await v.waitFor("link");
    assert.deepEqual(l2.link, { link_kind: "source", params: { path: "tex/source/a.tex", label: "sec:one" } });
    await clickText(v, "web");
    const l3 = await v.waitFor("link");
    assert.deepEqual([l3.kind, l3.target], ["web", "https://example.org/page"]);
    await v.page.evaluate(() => window.scrollTo(0, 0));
    await clickText(v, "table");
    const l4 = await v.waitFor("link", (m) => m.kind === "internal");
    assert.equal(l4.handled, true);
    const top = await v.page.evaluate(() => document.getElementById("sec-table").getBoundingClientRect().top);
    assert.ok(Math.abs(top - 8) < 2, "internal link scrolls to its target: " + top);
    await v.page.waitForTimeout(100);
    assert.equal(v.page.url(), url);
    assert.deepEqual(v.outside, []);
  } finally { await v.close(); }
});

test("synthetic: position round trip and scrollTo", async () => {
  const v = await openEdition(SYN);
  try {
    for (const y of [0, 333, 1250, 2100]) {
      await v.page.evaluate((y) => window.scrollTo(0, y), y);
      const p = await v.call("getPosition");
      assert.ok(p.source && p.source.source_file === "note.md", JSON.stringify(p));
      await v.page.evaluate(() => window.scrollTo(0, 0));
      await v.call("scrollTo", p);
      const back = await v.page.evaluate(() => window.scrollY);
      assert.ok(Math.abs(back - y) <= 2, `round trip from ${y}: ${back}`);
      const p2 = await v.call("getPosition");
      const same = (q) => ({ ...q, fraction: undefined, offset_y: undefined });
      assert.deepEqual(same(p2.source), same(p.source));
      assert.ok(Math.abs(p2.source.offset_y - p.source.offset_y) <= 2);
    }
    // Throttled position messages while scrolling.
    v.clear();
    await v.page.mouse.move(400, 500);
    await v.page.mouse.wheel(0, 600);
    const msg = await v.waitFor("position");
    assert.ok(msg.scroll_y > 0 && msg.source);
    // By anchor key and by byte.
    await v.call("scrollTo", "review:c2:answer:s1");
    const p3 = await v.call("getPosition");
    assert.equal(p3.anchor_key, "review:c2:answer:s1");
    const sec = synthetic.source.indexOf("Table section");
    await v.call("scrollTo", { sourceFile: "note.md", byte: Buffer.byteLength(synthetic.source.slice(0, sec)) });
    assert.ok(Math.abs(await v.page.evaluate(() => document.getElementById("sec-table").getBoundingClientRect().top)) < 30);
  } finally { await v.close(); }
});

test("synthetic: theme and text size", async () => {
  const v = await openEdition(SYN, { colorScheme: "dark", theme: "system" });
  try {
    const sys = await v.page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.equal(sys, "rgb(21, 21, 21)");
    const light = await v.call("setTheme", "light");
    assert.equal(light.background, "rgb(255, 255, 255)");
    const dark = await v.call("setTheme", "dark");
    assert.equal(dark.background, "rgb(21, 21, 21)");
    assert.equal(dark.color, "rgb(230, 230, 230)");
    await v.call("setItems", CARDS, COMMENTS, [], {});
    const n1 = (await v.call("presentReview", "c1", {})).blocks;
    assert.equal(await v.page.evaluate(() => getComputedStyle(document.querySelector(".tv-mask-block")).backgroundColor), "rgb(21, 21, 21)");
    const before = await v.page.evaluate(() => document.querySelector(".tv-mask-block").getBoundingClientRect().height);
    v.clear();
    const ts = await v.call("setTextSize", 150);
    assert.equal(ts.font_px, 25.5);
    const after = await v.page.evaluate(() => document.querySelector(".tv-mask-block").getBoundingClientRect().height);
    assert.ok(after > before * 1.3, `mask follows text size: ${before} -> ${after}`);
    assert.equal(await v.call("reviewMaskBlockCount"), n1);
    // Resize: recomputed in the same frame.
    // (A loaded Mac can deliver WebKit's resize event late: poll for up to 2 s.)
    await v.page.setViewportSize({ width: 420, height: 800 });
    let missed = null;
    for (const t0 = Date.now(); Date.now() - t0 < 2000;) {
      await v.page.waitForTimeout(100);
      missed = uncovered(await maskCoverage(v, ["review:c1:answer:s1"])).map((f) => f.word);
      if (!missed.length) { break; }
    }
    assert.deepEqual(missed, []);
  } finally { await v.close(); }
});

test("synthetic: highlight overlay fallback without the CSS Highlight API", async () => {
  const v = await openEdition(SYN, { load: { noHighlightApi: true } });
  try {
    await v.call("setItems", CARDS, COMMENTS, [], {});
    assert.ok(await v.page.locator(".tv-hl.tv-hl-comment").count() >= 1);
    assert.ok(await v.page.locator(".tv-hl.tv-hl-card").count() >= 4);
  } finally { await v.close(); }
});

test("synthetic: Android-style bridge object, custom transport, queue", async () => {
  const v = await openEdition(SYN);
  try {
    // A @JavascriptInterface object: window.<name>.postMessage(String).
    const got = await v.page.evaluate(() => {
      const seen = [];
      window.ThoughtsViewerBridge = { postMessage: (json) => seen.push(JSON.parse(json)) };
      const r = JSON.parse(window.ThoughtsViewer.call("load", JSON.stringify([{ transport: { kind: "android" }, selectionDelayMs: 0 }])));
      window.ThoughtsViewer.call("setItems", JSON.stringify([[{ id: "c3", anchors: ["review:c3:answer:s1"] }], [], []]));
      window.ThoughtsViewer.call("presentReview", JSON.stringify(["c3", { scroll: false }]));
      return { ok: r.ok, types: seen.map((m) => m.type) };
    });
    assert.equal(got.ok, true);
    assert.ok(got.types.includes("reviewMask"), got.types.join(","));
    // A host function replaces the configured transport.
    const custom = await v.page.evaluate(() => {
      const seen = [];
      window.ThoughtsViewer.setTransport((json) => seen.push(JSON.parse(json).type));
      window.ThoughtsViewer.call("reveal", "[]");
      return seen;
    });
    assert.deepEqual(custom, ["reviewMask"]);
    // Queue: undelivered messages wait for drain().
    const queued = await v.page.evaluate(() => {
      window.ThoughtsViewer.setTransport(null);
      window.ThoughtsViewer.call("load", JSON.stringify([{ transport: { kind: "queue" } }]));
      window.ThoughtsViewer.call("clearReview", "[]");
      return JSON.parse(window.ThoughtsViewer.drain()).map((m) => [m.type, m.v, typeof m.seq]);
    });
    assert.deepEqual(queued.find((m) => m[0] === "reviewMask"), ["reviewMask", 1, "number"]);
    // Calls before load and unknown calls fail cleanly.
    const errors = await v.page.evaluate(() => [JSON.parse(window.ThoughtsViewer.call("presentReview", "[\"nope\"]")),
      JSON.parse(window.ThoughtsViewer.call("scrollTo", "not json"))]);
    assert.equal(errors[0].ok, false);
    assert.equal(errors[1].ok, false);
  } finally { await v.close(); }
});

// ---- HTML attachments (mode "attachment") ------------------------------------------
// fixtures/attachment: a page whose text its own script renders after load, with tabs routed by
// location.hash and simulator results re-rendered on every slider input (like an explainer page).

const ATTACH = { "index.html": readFileSync(join(here, "fixtures/attachment/index.html")) };
const Q = {
  toll: { exact: "agents pay a toll of one cent per page", prefix: "Every crawler that wants to read this site has to pay first: ",
    suffix: " before the text is sent." },
  sim: { exact: "Every simulated agent pays the toll before it may read a single page of the site.", prefix: "dollars per hour. ", suffix: "" },
  agents40: { exact: "With 40 agents per hour", prefix: "Agents per hour ", suffix: " the site earns 0.40 dollars per hour." },
  missing: { exact: "This sentence is on none of the tabs." }
};

const rendered = (v, n = 1) => v.page.waitForFunction((n) => window.__page.renders >= n, n);
const setSlider = (v, n) => v.page.evaluate((n) => {
  const el = document.getElementById("agents");
  el.value = String(n);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}, n);
// Ranges of a highlight: text and whether they are in the live DOM.
const highlightRanges = (v, name) => v.page.evaluate((name) => {
  const h = CSS.highlights.get("tv-" + name);
  return h ? [...h].map((r) => ({ text: r.toString(), live: r.startContainer.isConnected && document.getElementById("app").contains(r.startContainer) })) : [];
}, name);
// Words of `exact` (one text node of the page) that no mask block covers; null when the text is not shown.
// With `slider`: first moves the slider (the page re-renders) and measures in the next frame.
const quoteCoverage = (v, exact, slider) => v.page.evaluate(async ([exact, slider]) => {
  if (slider !== undefined) {
    const old = [...document.querySelectorAll("#results p")].find((e) => e.textContent === exact);
    const top = old.getBoundingClientRect().top;
    const el = document.getElementById("agents");
    el.value = String(slider);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise((resolve) => requestAnimationFrame(() => resolve()));
    if (old.isConnected) { return "not re-rendered"; }
    const moved = [...document.querySelectorAll("#results p")].find((e) => e.textContent === exact).getBoundingClientRect().top - top;
    if (Math.abs(moved) < 20) { return "did not move: " + moved; }
  }
  const w = document.createTreeWalker(document.getElementById("app"), NodeFilter.SHOW_TEXT);
  let n, node = null;
  while ((n = w.nextNode())) { if (n.data.includes(exact)) { node = n; break; } }
  if (!node) { return null; }
  const start = node.data.indexOf(exact);
  const blocks = [...document.querySelectorAll(".tv-mask-block")].map((d) => d.getBoundingClientRect());
  const missed = [];
  const re = /\S+/g;
  let m;
  while ((m = re.exec(exact))) {
    const r = document.createRange();
    r.setStart(node, start + m.index);
    r.setEnd(node, start + m.index + m[0].length);
    for (const q of r.getClientRects()) {
      const cx = q.left + q.width / 2, cy = q.top + q.height / 2;
      if (!blocks.some((b) => cx >= b.left - 0.5 && cx <= b.right + 0.5 && cy >= b.top - 0.5 && cy <= b.bottom + 0.5)) { missed.push(m[0]); }
    }
  }
  return missed;
}, [exact, slider]);
const noErrors = (v) => {
  assert.deepEqual(v.outside, []);
  assert.deepEqual(v.messages.filter((m) => m.type === "error" || m.type === "page-error" || m.type === "console-error")
    .map((m) => m.message || m.text), []);
};

test("attachment: text rendered after load is placed; a re-render keeps placement and highlight", async () => {
  const v = await openEdition(ATTACH, { attachment: true });
  try {
    assert.equal(v.ready.mode, "attachment");
    assert.equal(v.ready.source_map, "none");
    const rep = await v.call("setItems", [{ id: "toll", anchors: [], text_quote: Q.toll }], [], [], {});
    if (!rep.cards.placed.includes("toll")) {
      // The page had not rendered yet: placed as soon as its text appears.
      assert.deepEqual(rep.cards.unplaced.map((u) => [u.id, u.reason]), [["toll", "quote_not_found"]]);
      const pl = await v.waitFor("placement", (m) => m.cards.placed.includes("toll"), 5000);
      assert.deepEqual(pl.cards.unplaced, []);
    }
    await rendered(v);
    assert.deepEqual(await highlightRanges(v, "card"), [{ text: Q.toll.exact, live: true }]);
    assert.deepEqual(await highlightRanges(v, "pending"), []);
    // The page replaces its nodes (same text): the highlight moves to the new nodes.
    await v.page.evaluate(() => { window.__old = [...CSS.highlights.get("tv-card")][0].startContainer; });
    await v.page.click("#refresh");
    await v.page.waitForFunction((exact) => {
      const h = CSS.highlights.get("tv-card");
      const r = h && [...h][0];
      return window.__page.refreshes === 1 && !window.__old.isConnected && r && r.toString() === exact && r.startContainer.isConnected;
    }, Q.toll.exact, { timeout: 3000 });
    // reindex() on demand.
    const ri = await v.call("reindex");
    assert.ok(ri.text_length > 100, JSON.stringify(ri));
    assert.deepEqual(ri.placement.cards.placed, ["toll"]);
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: hash-routed tabs, slider re-render moves highlights, dynamic text unplaced and placed again", async () => {
  const v = await openEdition(ATTACH, { attachment: true });
  try {
    await rendered(v);
    v.clear();
    // The page's own tab link: not cancelled, its click listener runs, hashchange routes.
    await v.page.click("#tabs a[data-tab=simulator]");
    await v.page.waitForFunction(() => window.__page.hashchanges === 1 && document.getElementById("agents"));
    assert.deepEqual(await v.page.evaluate(() => [location.hash, window.__page.navClicks]), ["#simulator", 1]);
    const link = await v.waitFor("link");
    assert.deepEqual([link.kind, link.target, link.passive, link.handled], ["internal", "#simulator", true, false]);
    await v.waitFor("position", (m) => m.page_state && m.page_state.hash === "#simulator");
    const rep = await v.call("setItems", [{ id: "sim", anchors: [], text_quote: Q.sim }, { id: "n40", anchors: [], text_quote: Q.agents40 }], [], [], {});
    assert.deepEqual(rep.cards.placed.slice().sort(), ["n40", "sim"]);
    v.clear();
    await setSlider(v, 60);
    const pl = await v.waitFor("placement", () => true, 3000);
    assert.deepEqual(pl.cards.placed, ["sim"]);
    assert.deepEqual(pl.cards.unplaced.map((u) => [u.id, u.reason]), [["n40", "quote_not_found"]]);
    assert.deepEqual(await highlightRanges(v, "card"), [{ text: Q.sim.exact, live: true }]);
    await setSlider(v, 40);
    const pl2 = await v.waitFor("placement", () => true, 3000);
    assert.deepEqual(pl2.cards.placed.slice().sort(), ["n40", "sim"]);
    const texts = (await highlightRanges(v, "card")).map((r) => r.live && r.text).sort();
    assert.deepEqual(texts, [Q.sim.exact, Q.agents40.exact].sort());
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: overlay highlights follow a re-render (no Highlight API)", async () => {
  const v = await openEdition(ATTACH, { attachment: true, hash: "#simulator", load: { noHighlightApi: true } });
  try {
    await rendered(v);
    await v.call("setItems", [{ id: "sim", anchors: [], text_quote: Q.sim }], [], [], {});
    const boxes = () => v.page.evaluate((exact) => {
      const p = [...document.querySelectorAll("#results p")].find((e) => e.textContent === exact);
      const t = p.getBoundingClientRect();
      return { text: [t.top, t.bottom], hl: [...document.querySelectorAll(".tv-hl.tv-hl-card")].map((d) => d.getBoundingClientRect()).map((q) => [q.top, q.bottom]) };
    }, Q.sim.exact);
    const near = (b) => b.hl.length >= 1 && b.hl.every((h) => h[0] >= b.text[0] - 2 && h[1] <= b.text[1] + 2);
    assert.ok(near(await boxes()), JSON.stringify(await boxes()));
    // A longer result line wraps at phone width and pushes the sentence down.
    await v.page.setViewportSize({ width: 360, height: 800 });
    await setSlider(v, 100);
    await v.page.waitForTimeout(600);
    assert.ok(near(await boxes()), JSON.stringify(await boxes()));
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: setPageState + presentReview masks the answer on a hash-routed tab, also across re-renders", async () => {
  const v = await openEdition(ATTACH, { attachment: true });
  try {
    await rendered(v);
    const rep = await v.call("setItems", [{ id: "sim", anchors: [], text_quote: Q.sim }], [], [], {});
    assert.deepEqual(rep.cards.unplaced.map((u) => u.id), ["sim"]);
    v.clear();
    // Both calls in one task (before the page's hashchange runs): the answer is waited for
    // while the page renders the tab.
    const [ps, pr] = await v.page.evaluate(() => [
      JSON.parse(window.ThoughtsViewer.call("setPageState", JSON.stringify([{ hash: "#simulator" }]))),
      JSON.parse(window.ThoughtsViewer.call("presentReview", JSON.stringify(["sim", {}])))]);
    assert.deepEqual(ps, { ok: true, result: { page_state: { hash: "#simulator" }, changed: true } });
    assert.deepEqual(pr, { ok: true, result: { blocks: 0, pending: true } });
    const m = await v.waitFor("reviewMask", (x) => x.state === "hidden", 6000);
    assert.equal(m.found, true);
    assert.equal(m.card_id, "sim");
    assert.ok(m.blocks >= 1);
    const pst = await v.waitFor("pageState", () => true, 6000);
    assert.deepEqual([pst.page_state, pst.changed, pst.settled], [{ hash: "#simulator" }, true, true]);
    assert.deepEqual(await quoteCoverage(v, Q.sim.exact), []);
    // The rest of the tab stays visible.
    assert.equal((await quoteCoverage(v, "With 40 agents per hour")).length, 5);
    // The page re-renders the answer while it is hidden: masked again before the next frame.
    await v.page.waitForTimeout(150);
    assert.deepEqual(await quoteCoverage(v, Q.sim.exact, 70), []);
    await v.page.waitForTimeout(400);
    assert.deepEqual(await quoteCoverage(v, Q.sim.exact), []);
    assert.equal(await v.call("reviewMaskBlockCount"), m.blocks);
    // Reveal, then a selection in the answer carries the page state and the card.
    await v.call("reveal");
    assert.equal(await v.call("selectQuote", Q.sim), true);
    const sel = await v.call("getSelection");
    assert.deepEqual(sel.page_state, { hash: "#simulator" });
    assert.equal(sel.text_quote.exact, Q.sim.exact);
    assert.equal(sel.answer && sel.answer.card_id, "sim");
    assert.deepEqual((await v.call("getPosition")).page_state, { hash: "#simulator" });
    assert.deepEqual(await v.call("getPageState"), { hash: "#simulator" });
    // Back to the first tab: no hash.
    await v.call("clearReview");
    assert.equal((await v.call("setPageState", { hash: "" })).changed, true);
    await v.waitFor("pageState", (x) => x.page_state.hash === "", 6000);
    assert.ok(await v.page.evaluate(() => !!document.getElementById("refresh")));
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: answer not in the page → reviewMask not_found", async () => {
  const v = await openEdition(ATTACH, { attachment: true, load: { reviewTimeoutMs: 400 } });
  try {
    await rendered(v);
    await v.call("setItems", [{ id: "gone", anchors: [], text_quote: Q.missing }, { id: "toll", anchors: [], text_quote: Q.toll }], [], [], {});
    v.clear();
    assert.deepEqual(await v.call("presentReview", "gone", {}), { blocks: 0, pending: true });
    const m = await v.waitFor("reviewMask", (x) => x.card_id === "gone", 8000);
    assert.deepEqual([m.state, m.found, m.blocks], ["not_found", false, 0]);
    assert.equal(await v.call("reviewMaskBlockCount"), 0);
    assert.equal((await v.call("info")).review, null);
    assert.equal(await v.page.locator(".tv-mask-block").count(), 0);
    // A card the app did not pass, without a quote, is an error as in an edition.
    assert.equal((await v.callRaw("presentReview", "nope", {})).ok, false);
    // A quote given to presentReview itself.
    v.clear();
    const res = await v.call("presentReview", "adhoc", { text_quote: Q.toll });
    assert.ok(res.blocks >= 1, JSON.stringify(res));
    const h = await v.waitFor("reviewMask", (x) => x.card_id === "adhoc");
    assert.deepEqual([h.state, h.found], ["hidden", true]);
    assert.deepEqual(await quoteCoverage(v, Q.toll.exact), []);
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: forwardKeys after the page's listeners; the page keeps keys it cancels and its controls' keys", async () => {
  const v = await openEdition(ATTACH, { attachment: true, load: { forwardKeys: ["s", "k", "j", "Escape", "Space", "ArrowRight", "Meta+Shift+u"] } });
  try {
    await rendered(v);
    let from = 0;
    const forwarded = () => v.messages.slice(from).filter((m) => m.type === "key" && m.forwarded).map((m) => m.key.toLowerCase());
    await v.page.evaluate(() => {
      window.__seen = [];
      // The page's own shortcuts: it cancels k, stops j, only looks at s.
      document.addEventListener("keydown", (e) => {
        if (["Shift", "Meta", "Control", "Alt"].includes(e.key)) { return; }
        window.__seen.push(e.key.toLowerCase());
        if (e.key === "k") { e.preventDefault(); }
        if (e.key === "j") { e.stopPropagation(); }
      });
      document.activeElement && document.activeElement.blur && document.activeElement.blur();
    });
    from = v.messages.length;
    await v.page.keyboard.press("s");
    const s = await v.waitFor("key", (m) => m.key === "s");
    assert.deepEqual([s.forwarded, s.repeat], [true, false]);
    await v.page.keyboard.press("k");
    await v.page.keyboard.press("j");
    await v.page.keyboard.press("Escape");
    await v.waitFor("key", (m) => m.key === "Escape");
    await v.page.keyboard.press("Meta+Shift+KeyU");
    assert.equal((await v.waitFor("key", (m) => m.code === "KeyU")).forwarded, true);
    // Space on the page (not on a control): forwarded and cancelled (no scroll).
    const y0 = await v.page.evaluate(() => window.scrollY);
    await v.page.keyboard.press("Space");
    await v.waitFor("key", (m) => m.code === "Space");
    assert.equal(await v.page.evaluate(() => window.scrollY), y0);
    // The page saw every key first, also the forwarded ones.
    assert.deepEqual(await v.page.evaluate(() => window.__seen), ["s", "k", "j", "escape", "u", " "]);
    assert.deepEqual(forwarded(), ["s", "escape", "u", " "]);
    // Space on the page's focused button presses it; S there still goes to the app.
    from = v.messages.length;
    await v.page.focus("#refresh");
    await v.page.keyboard.press("Space");
    await v.page.waitForFunction(() => window.__page.refreshes === 1);
    await rendered(v, 2);
    await v.page.focus("#refresh");
    await v.page.keyboard.press("s");
    await v.waitFor("key", (m) => m.key === "s");
    assert.deepEqual(forwarded(), ["s"]);
    // ArrowRight on the focused slider moves it (the page re-renders); not forwarded.
    assert.equal((await v.call("setPageState", { hash: "#simulator" })).changed, true);
    await v.page.waitForFunction(() => !!document.getElementById("agents"));
    from = v.messages.length;
    await v.page.focus("#agents");
    await v.page.keyboard.press("ArrowRight");
    await v.page.waitForFunction(() => window.__page.agents === 50);
    await v.page.waitForTimeout(150);
    assert.deepEqual(forwarded(), []);
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: a selection from the answer's first character (or the gap before it) gets the answer, also after a re-render", async () => {
  const v = await openEdition(ATTACH, { attachment: true, hash: "#simulator" });
  try {
    await rendered(v);
    await v.call("setItems", [{ id: "sim", anchors: [], text_quote: Q.sim }], [], [], {});
    await v.call("presentReview", "sim", {});
    await v.waitFor("reviewMask", (x) => x.state === "hidden", 6000);
    await v.call("reveal");
    // The answer is a paragraph of its own (placed by quote, no markers): the collapsed space
    // before it maps to its offset 0, where the selection starts too.
    const selectAnswer = () => v.page.evaluate((exact) => {
      const p = [...document.querySelectorAll("#results p")].find((e) => e.textContent === exact);
      const r = document.createRange();
      r.setStart(p.firstChild, 0);
      r.setEnd(p.firstChild, p.firstChild.data.length);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }, Q.sim.exact);
    await selectAnswer();
    let sel = await v.call("getSelection");
    // A drag from the gap before it: the range starts at the end of the list's last item.
    await v.page.evaluate((exact) => {
      const p = [...document.querySelectorAll("#results p")].find((e) => e.textContent === exact);
      const li = [...document.querySelectorAll("#results li")].at(-1).firstChild;
      const r = document.createRange();
      r.setStart(li, li.data.length);
      r.setEnd(p.firstChild, p.firstChild.data.length);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }, Q.sim.exact);
    sel = await v.call("getSelection");
    assert.equal(sel.text_quote.exact, Q.sim.exact);
    assert.equal(sel.answer && sel.answer.card_id, "sim");
    await selectAnswer();
    sel = await v.call("getSelection");
    assert.equal(sel.text_quote.exact, Q.sim.exact);
    assert.equal(sel.answer && sel.answer.card_id, "sim");
    assert.equal(sel.answer.text_quote.exact, Q.sim.exact);
    // The page re-renders the answer as new nodes.
    await setSlider(v, 80);
    await v.page.waitForTimeout(400);
    await selectAnswer();
    sel = await v.call("getSelection");
    assert.equal(sel.answer && sel.answer.card_id, "sim");
    assert.equal(sel.answer.text_quote.exact, Q.sim.exact);
    noErrors(v);
  } finally { await v.close(); }
});

test("attachment: document feedback is found in the whole page, follows a re-render, and can open the app's menu", async () => {
  const v = await openEdition(ATTACH, { attachment: true, hash: "#simulator", load: { selectionMenu: "app" } });
  try {
    await rendered(v);
    const quote = "Every simulated agent pays the toll";
    const rep = await v.call("setItems", [], [], [
      { id: "liked", polarity: "positive", text_quote: { exact: quote, prefix: "", suffix: "" } },
      { id: "gone", polarity: "negative", text_quote: { exact: "text that is on no tab of this page", prefix: "", suffix: "" } },
      { id: "noquote", polarity: "negative" }],
      { show_feedback: "always" });
    assert.deepEqual(rep.feedback.placed, ["liked"]);
    assert.deepEqual(rep.feedback.unplaced.map((u) => [u.id, u.reason]), [["gone", "orphaned"], ["noquote", "no_quote"]]);
    assert.equal(await v.page.evaluate(() => CSS.highlights.has("tv-feedback-positive")), true);
    assert.equal(await v.page.evaluate(() => [...CSS.highlights.get("tv-feedback-positive")][0].toString()), quote);
    // A question or another comment has a colour of its own.
    await v.call("setItems", [], [], [{ id: "asked", polarity: "neutral", text_quote: { exact: quote, prefix: "", suffix: "" } }], { show_feedback: "always" });
    assert.equal(await v.page.evaluate(() => [...CSS.highlights.get("tv-feedback-neutral")][0].toString()), quote);
    await v.call("setItems", [], [], [{ id: "liked", polarity: "positive", text_quote: { exact: quote, prefix: "", suffix: "" } }], { show_feedback: "always" });
    // The page rebuilds that sentence: the highlight follows it.
    await setSlider(v, 60);
    await v.page.waitForFunction((q) => {
      const r = [...(CSS.highlights.get("tv-feedback-positive") || [])][0];
      return r && r.toString() === q && r.startContainer.isConnected;
    }, quote);
    assert.equal((await v.call("scrollTo", { item_id: "liked", kind: "feedback" })).found, true);

    // A tap on the highlight, then a right click on it: the app's menu, not the page's.
    const box = await v.page.evaluate(() => {
      const r = [...CSS.highlights.get("tv-feedback-positive")][0].getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    });
    await v.page.mouse.click(box.x, box.y);
    const tapped = await v.waitFor("itemTapped");
    assert.deepEqual([tapped.kind, tapped.id], ["feedback", "liked"]);
    await v.page.mouse.click(box.x, box.y, { button: "right" });
    const menu = await v.waitFor("contextmenu");
    assert.equal(menu.app_menu, true);
    assert.deepEqual(menu.items.map((i) => [i.kind, i.id]), [["feedback", "liked"]]);
    assert.deepEqual(await v.page.evaluate(() => [window.__page.contextmenus, window.__page.contextmenuPrevented]), [1, true]);
  } finally { await v.close(); }
});

test("attachment: page links, hash links, context menu, drag stay the page's; no theme or text size", async () => {
  const v = await openEdition(ATTACH, { attachment: true, theme: "dark" });
  try {
    await rendered(v);
    const look = () => v.page.evaluate(() => ({ cls: document.documentElement.className, bg: getComputedStyle(document.body).backgroundColor,
      font: getComputedStyle(document.body).fontSize }));
    assert.deepEqual(await look(), { cls: "", bg: "rgb(251, 250, 247)", font: "17px" });
    const th = await v.call("setTheme", "dark");
    assert.deepEqual([th.applied, th.background], [false, "rgb(251, 250, 247)"]);
    const ts = await v.call("setTextSize", 150);
    assert.deepEqual([ts.applied, ts.text_size, ts.font_px], [false, 100, 17]);
    assert.deepEqual(await look(), { cls: "", bg: "rgb(251, 250, 247)", font: "17px" });
    // The mask is the page's background.
    await v.call("setItems", [{ id: "toll", anchors: [], text_quote: Q.toll }], [], [], {});
    await v.call("presentReview", "toll", {});
    assert.equal(await v.page.evaluate(() => getComputedStyle(document.querySelector(".tv-mask-block")).backgroundColor), "rgb(251, 250, 247)");
    await v.call("clearReview");
    // A press on the page's controls is the page's; plain highlighted text is a tap on the card.
    await v.call("setItems", [{ id: "toll", anchors: [], text_quote: Q.toll },
      { id: "button", anchors: [], text_quote: { exact: "Shuffle the examples" } }], [], [], {});
    v.clear();
    await clickText(v, "Shuffle the examples");
    await v.page.waitForFunction(() => window.__page.refreshes === 1);
    await clickText(v, "toll of one cent");
    const tap = await v.waitFor("itemTapped");
    assert.equal(tap.id, "toll");
    assert.equal(v.messages.filter((m) => m.type === "itemTapped").length, 1);
    // A link with the page's own click handler (bubbling, preventDefault by the page).
    v.clear();
    await v.page.click("#js-link");
    assert.equal(await v.page.evaluate(() => window.__page.clicks), 1);
    assert.equal((await v.waitFor("link")).passive, true);
    // A #hash link: the browser's default action, the page's hashchange.
    await v.page.click("#activity-link");
    await v.page.waitForFunction(() => location.hash === "#activity" && window.__page.hashchanges === 1);
    await v.page.waitForFunction(() => document.getElementById("app").textContent.includes("My activity"));
    // Context menu: not cancelled, the page's listener sees it, the app still gets the message.
    v.clear();
    await clickText(v, "You opened this page", "right");
    const menu = await v.waitFor("contextmenu");
    assert.equal(menu.point.length, 2);
    assert.deepEqual(await v.page.evaluate(() => [window.__page.contextmenus, window.__page.contextmenuPrevented]), [1, false]);
    // dragstart on a link: not cancelled.
    const drag = await v.page.evaluate(() => {
      const ev = new Event("dragstart", { bubbles: true, cancelable: true });
      document.getElementById("activity-link").dispatchEvent(ev);
      return [ev.defaultPrevented, window.__page.drags];
    });
    assert.deepEqual(drag, [false, 1]);
    noErrors(v);
  } finally { await v.close(); }
});

// ---- golden editions -------------------------------------------------------------
// The server-built golden editions in tests/golden (synthetic-latex, synthetic-markdown,
// thoughts_manual: copies of the server's tests/fixtures/html-editions), each with its
// manifest.json. --goldens DIR / THOUGHTS_HTML_GOLDENS points elsewhere (e.g. fresher builds).

const goldensDir = opt("goldens", process.env.THOUGHTS_HTML_GOLDENS || join(here, "golden"));

function goldenEditions() {
  const list = [];
  if (existsSync(goldensDir)) {
    for (const d of readdirSync(goldensDir).sort()) {
      const dir = join(goldensDir, d);
      if (existsSync(join(dir, "index.html")) && existsSync(join(dir, "manifest.json"))) {
        list.push({ name: d, dir, manifest: JSON.parse(readFileSync(join(dir, "manifest.json"), "utf8")) });
      }
    }
  }
  if (existsSync(samplesDir)) {
    for (const d of readdirSync(samplesDir).sort()) {
      const dir = join(samplesDir, d, "tex4ht");
      if (existsSync(join(dir, "index.html")) && !d.includes("thoughts_manual")) { list.push({ name: "spike " + d, dir, manifest: null }); }
    }
  }
  return list;
}

const GOLDENS = goldenEditions();
test("golden editions are present", async () => {
  assert.ok(GOLDENS.filter((g) => g.manifest).length >= 3,
    `no server-built golden editions in ${goldensDir} (use --goldens DIR)`);
});

// Cards and comments as the apps pass them: from the manifest's items, so no key is parsed.
// Spike samples have no items: every start/end key is a card, every point key a comment.
function goldenItems(ed, html) {
  const starts = [...new Set([...html.matchAll(/data-tm="start" data-key="([^"]+)"/g)].map((m) => m[1]))];
  const points = [...new Set([...html.matchAll(/data-tm="point" data-key="([^"]+)"/g)].map((m) => m[1]))];
  if (!ed.manifest || !ed.manifest.items) {
    return { anchorCount: starts.length + points.length, sourceMap: "lines",
      cards: starts.map((k) => ({ id: k, anchors: [k] })), comments: points.map((k) => ({ id: k, anchors: [k] })) };
  }
  const items = ed.manifest.items;
  return {
    anchorCount: starts.length + points.length,
    sourceMap: ed.manifest.source_map,
    cards: Object.entries(items.cards || {}).map(([id, c]) => ({ id, anchors: c.anchors, question_anchors: c.question_anchors || [] })),
    comments: Object.entries(items.comments || {}).map(([id, anchors]) => ({ id, anchors })),
  };
}

for (const ed of GOLDENS) {
  test("golden " + ed.name, async () => {
    const html = readFileSync(join(ed.dir, "index.html"), "utf8");
    const { anchorCount, sourceMap, cards, comments } = goldenItems(ed, html);
    if (ed.manifest) {
      // Every anchor the manifest lists is in the HTML, and every marker of the HTML is listed.
      const inHtml = new Set([...html.matchAll(/data-key="([^"]+)"/g)].map((m) => m[1]));
      assert.deepEqual([...inHtml].sort(), [...ed.manifest.anchors].sort());
    }
    const v = await openEdition(ed.dir);
    try {
      assert.equal(v.ready.source_map, sourceMap);
      assert.equal(v.ready.anchors.length, anchorCount);
      const rep = await v.call("setItems", cards, comments, [], {});
      assert.deepEqual(rep.cards.placed.slice().sort(), cards.map((c) => c.id).sort());
      assert.deepEqual(rep.cards.unplaced, []);
      assert.deepEqual(rep.comments.placed.slice().sort(), comments.map((c) => c.id).sort());
      const counts = [];
      for (const card of cards) {
        const res = await v.call("presentReview", card.id, {});
        const cov = await maskCoverage(v, card.anchors);
        const missed = uncovered(cov).filter((f) => !f.clipped);
        assert.deepEqual(missed.map((f) => f.word), [], `${card.id}: answer words outside the mask`);
        assert.ok(res.blocks >= 1);
        counts.push(res.blocks);
        // Selecting inside the (revealed) answer gives a source position and the card.
        await v.call("reveal");
        const k = card.anchors[0];
        const ok = await v.page.evaluate((k) => {
          const s = document.querySelector(`[data-tm="start"][data-key="${k}"]`);
          const e = document.querySelector(`[data-tm="end"][data-key="${k}"]`);
          const r = document.createRange(); r.setStartAfter(s); r.setEndBefore(e);
          getSelection().removeAllRanges(); getSelection().addRange(r);
          return String(getSelection()).trim().length > 0;
        }, k);
        assert.ok(ok, `${card.id}: empty answer`);
        const sel = await v.call("getSelection");
        if (sourceMap === "lines") {
          assert.ok(sel.source_lines && sel.source_lines.first_line >= 1, JSON.stringify(sel.source_lines));
        } else {
          const sr = sel.source_range;
          assert.ok(sr && sr.end_byte > sr.start_byte, JSON.stringify(sr));
          assert.ok(sr.source_file in (ed.manifest.source_files || {}), `${sr.source_file} not in source_files`);
          assert.equal(typeof sr.exact, "boolean", JSON.stringify(sr));
          // Plain words are their own source: exact bytes.
          if (/^[A-Za-z0-9 ]+$/.test(sel.text_quote.exact)) { assert.equal(sr.exact, true, JSON.stringify(sr)); }
        }
        assert.ok(sel.text_quote && sel.text_quote.exact.length > 0);
        assert.equal(sel.answer && sel.answer.card_id, card.id);
        await v.call("clearSelection");
        await v.call("clearReview");
      }
      if (cards.length) { console.log(`       ${ed.name}: mask blocks per card ${JSON.stringify(counts)}`); }
      // Every card of these editions is one passage (headings, paragraphs, lists): one block.
      assert.deepEqual(counts, cards.map(() => 1));
      // Phone width: the mask still covers every answer word.
      await v.page.setViewportSize({ width: 412, height: 860 });
      for (const card of cards) {
        await v.call("presentReview", card.id, {});
        const missed = uncovered(await maskCoverage(v, card.anchors)).filter((f) => !f.clipped);
        assert.deepEqual(missed.map((f) => f.word), [], `${card.id} at 412 px`);
        await v.call("clearReview");
      }
      await v.page.setViewportSize({ width: 900, height: 1000 });
      // Position round trip at a few places (short editions may not scroll).
      const height = await v.page.evaluate(() => document.documentElement.scrollHeight - innerHeight);
      for (const f of height > 50 ? [0.1, 0.37, 0.8] : []) {
        const y = Math.round(height * f);
        await v.page.evaluate((y) => window.scrollTo(0, y), y);
        const p = await v.call("getPosition");
        assert.ok(p.source && p.source.source_file, JSON.stringify(p));
        await v.page.evaluate(() => window.scrollTo(0, 0));
        await v.call("scrollTo", p);
        const back = await v.page.evaluate(() => window.scrollY);
        assert.ok(Math.abs(back - y) <= 3, `round trip ${y} -> ${back}`);
        if (sourceMap === "lines") {
          // Line only (as from the PDF view): lands on the same block.
          assert.ok(p.source.line >= 1, JSON.stringify(p));
          await v.call("scrollTo", { source_file: p.source.source_file, line: p.source.line });
          const p2 = await v.call("getPosition");
          assert.ok(Math.abs(p2.source.line - p.source.line) <= Math.max(2, (p.source.last_line - p.source.first_line)),
            `line ${p.source.line} -> ${p2.source.line}`);
        }
      }
      // Links.
      // Links: the editions mark Thoughts links with data-thoughts-link (wins over href).
      const links = v.page.locator("[data-thoughts-link]");
      const linkCount = await links.count();
      if (ed.manifest) {
        assert.equal(linkCount, (html.match(/data-thoughts-link=/g) || []).length);
      }
      for (let i = 0; i < Math.min(linkCount, 3); i++) {
        v.clear();
        const want = await links.nth(i).getAttribute("data-thoughts-link");
        await links.nth(i).scrollIntoViewIfNeeded();
        await links.nth(i).click();
        const l = await v.waitFor("link");
        assert.equal(l.kind, "thoughts");
        assert.equal(l.target, want);
        assert.equal(l.link && l.link.link_kind, /^thoughts-link:([A-Za-z0-9_-]+)/.exec(want)[1]);
        assert.ok(l.handled === false || l.handled === undefined, JSON.stringify(l));
      }
      // Still on the edition (the click did not navigate).
      assert.equal(await v.page.evaluate(() => location.href), ORIGIN + "/index.html");
      // Dark theme.
      assert.equal((await v.call("setTheme", "dark")).background, "rgb(21, 21, 21)");
      const dark = await v.page.evaluate(() => ({ cls: document.documentElement.className,
        bg: getComputedStyle(document.body).backgroundColor, fg: getComputedStyle(document.body).color }));
      assert.ok(dark.cls.includes("tv-theme-dark"), dark.cls);
      assert.notEqual(dark.fg, dark.bg);
      // The mask follows the theme, and still covers every answer.
      for (const card of cards) {
        await v.call("presentReview", card.id, {});
        const missed = uncovered(await maskCoverage(v, card.anchors)).filter((f) => !f.clipped);
        assert.deepEqual(missed.map((f) => f.word), [], `${card.id} in dark mode`);
        await v.call("clearReview");
      }
      assert.equal((await v.call("setTheme", "light")).background !== "rgb(21, 21, 21)", true);
      assert.deepEqual(v.outside, []);
      assert.deepEqual(v.messages.filter((m) => m.type === "error" || m.type === "page-error").map((m) => m.message || m.text), []);
    } finally { await v.close(); }
  });
}

// ---- run ---------------------------------------------------------------------------

const pw = await loadPlaywright();
browser = await pw[browserName].launch();
console.log(`html-viewer tests: ${browserName} ${browser.version()}`);
let failed = 0;
for (const t of tests) {
  if (only && !t.name.includes(only)) { continue; }
  const started = Date.now();
  try {
    await t.fn();
    console.log(`ok   ${t.name} (${Date.now() - started} ms)`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${t.name}: ${e && e.stack ? e.stack.split("\n").slice(0, process.env.VERBOSE ? 60 : 4).join("\n     ") : e}`);
  }
}
await browser.close();
console.log(failed ? `${failed} test(s) failed` : "all passed");
process.exit(failed ? 1 : 0);
