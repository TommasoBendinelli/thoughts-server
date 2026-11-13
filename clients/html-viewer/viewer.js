// Thoughts HTML viewer: the shared reader for HTML editions (desktop and Android).
//
// The app loads the edition's index.html (script-free) from its local cache, then
// injects geometry.js, text.js and this file (runJavaScript / evaluateJavascript;
// works under CSP script-src 'none') and calls
//   ThoughtsViewer.call("load", JSON.stringify([{transport: …, css: <viewer.css>, …}]))
// Every app → viewer call is ThoughtsViewer.call(name, argsJson) and returns a JSON
// string {"ok":true,"result":…} or {"ok":false,"error":"…"}; viewer → app messages
// are JSON objects {type, v, seq, …} handed to the transport. The protocol is
// described in README.md next to this file.
//
// Strict-mode script without import/export (see geometry.js). Defines
// globalThis.ThoughtsViewer; a second injection keeps the existing instance.
(function (root) {
  "use strict";
  if (root.ThoughtsViewer && root.ThoughtsViewer.version) { return; }

  const VERSION = "0.3.1";
  const PROTOCOL = 1;
  const G = root.ThoughtsViewerGeometry;
  const T = root.ThoughtsViewerText;
  const doc = root.document;
  if (!G || !T) { throw new Error("ThoughtsViewer needs geometry.js and text.js injected first"); }

  const QUOTE_CONTEXT = 64;           // text_quote prefix/suffix for new cards/comments
  const ANSWER_QUOTE_CONTEXT = 32;    // answer feedback (desktop reviewcardfeedback.h contextLength)
  const FOCUS_MARGIN = 0.12;          // presentReview / focusReviewMaskBlock: block top at 12% of the view
  // Attachment page controls: a click on them is never an itemTapped.
  const INTERACTIVE = "a, button, input, select, textarea, label, summary, option, [contenteditable], [role=button], [role=tab], " +
    "[role=link], [role=checkbox], [role=switch], [role=slider], [role=menuitem], [onclick]";
  const ATOMIC = new Set(["svg", "img", "video", "canvas", "math", "iframe", "object", "embed", "input", "textarea", "select", "picture"]);

  const DEFAULT_STYLES = {
    "card": { background: "rgba(53,132,228,0.13)", dark: { background: "rgba(120,170,255,0.20)" } },
    "card-selected": { background: "rgba(25,95,190,0.28)", dark: { background: "rgba(110,160,255,0.38)" } },
    "pending": { background: "rgba(140,140,140,0.20)", underline: "rgba(120,120,120,0.9)" },
    "question": { background: "rgba(150,90,200,0.14)", dark: { background: "rgba(190,140,240,0.22)" } },
    "comment": { background: "rgba(255,196,0,0.30)", dark: { background: "rgba(255,200,40,0.28)" } },
    "comment-strike": { background: "rgba(255,120,80,0.18)", strike: true },
    "comment-selected": { background: "rgba(255,170,0,0.50)" },
    "feedback-positive": { background: "rgba(46,160,67,0.25)", underline: "rgb(46,160,67)" },
    "feedback-negative": { background: "rgba(220,50,47,0.22)", underline: "rgb(220,50,47)" },
    "feedback-neutral": { background: "rgba(21,101,192,0.20)", underline: "rgb(21,101,192)" }
  };
  // Later names paint over earlier ones.
  const STYLE_PRIORITY = ["card", "pending", "question", "card-selected", "comment-strike", "comment", "comment-selected",
    "feedback-positive", "feedback-negative", "feedback-neutral"];

  const state = {
    loaded: false,
    options: {},
    seq: 0,
    queue: [],
    transport: null,
    customTransport: null,
    baseSheet: null,
    dynSheet: null,
    styleEl: null,
    listeners: false,
    index: null,
    anchors: new Map(),         // key -> {start, end, point}
    anchorList: [],             // marker elements in document order
    warnings: [],
    items: new Map(),           // "kind:id" -> placement
    styles: Object.assign({}, DEFAULT_STYLES),
    highlightCards: true,
    feedbackMode: "revealed",
    theme: "system",
    textPercent: 100,
    baseFontPx: 0,
    layer: null,
    hlLayer: null,
    maskLayer: null,
    review: null,
    lastSelectionJson: "",
    selectionTimer: 0,
    positionTimer: 0,
    lastPositionJson: "",
    lastPositionSent: 0,
    rafPending: false,
    layoutClip: null,
    srcLists: null,
    resizeObserver: null,
    // Attachment mode (pages that run their own scripts): see load().
    mode: "edition",
    itemInputs: null,           // last setItems cards/comments/feedback, re-placed after DOM changes
    lastReportJson: "",
    mutationObserver: null,
    dirty: false,               // the page's DOM changed since the text index was built
    dirtySince: 0,
    lastMutation: 0,
    lastFlush: 0,
    reindexTimer: 0,
    observedBody: null,
    pageStateGen: 0
  };

  // ---- transport ------------------------------------------------------------

  function makeTransport(spec) {
    if (!spec || spec.kind === "queue") { return null; }
    if (spec.kind === "http") {
      const url = spec.url;
      if (!url) { throw new Error("http transport needs url"); }
      return function (json, msg) {
        root.fetch(url, { method: "POST", body: json, headers: { "Content-Type": "application/json" } })
          .catch(function () { state.queue.push(msg); });
      };
    }
    if (spec.kind === "android") {
      const name = spec.object || "ThoughtsViewerBridge";
      return function (json) {
        const bridge = root[name];
        if (!bridge || typeof bridge.postMessage !== "function") { throw new Error("no Android bridge " + name); }
        bridge.postMessage(json);
      };
    }
    if (spec.kind === "webkit") {
      const handler = spec.handler || "thoughts";
      return function (json) { root.webkit.messageHandlers[handler].postMessage(json); };
    }
    if (spec.kind === "webview2") {
      return function (json) { root.chrome.webview.postMessage(json); };
    }
    throw new Error("unknown transport " + spec.kind);
  }

  function send(type, payload) {
    const msg = Object.assign({ type: type, v: PROTOCOL, seq: ++state.seq }, payload || {});
    const fn = state.customTransport || state.transport;
    if (fn) {
      try {
        fn(JSON.stringify(msg), msg);
        return;
      } catch (e) {
        msg.transport_error = String(e && e.message || e);
      }
    }
    state.queue.push(msg);
    if (state.queue.length > 500) { state.queue.splice(0, state.queue.length - 500); }
  }

  function reportError(where, e) {
    try { send("error", { message: String(e && e.message || e), where: where }); } catch (ignored) { /* nothing left */ }
  }

  function guarded(where, fn) {
    return function (ev) {
      try { return fn(ev); } catch (e) { reportError(where, e); }
    };
  }

  // ---- styles ---------------------------------------------------------------

  function makeSheet(text) {
    try {
      const sheet = new root.CSSStyleSheet();
      sheet.replaceSync(text);
      doc.adoptedStyleSheets = doc.adoptedStyleSheets.concat([sheet]);
      return { sheet: sheet };
    } catch (e) {
      // Constructable sheets unsupported: a <style> element (needs style-src 'unsafe-inline').
      const el = doc.createElement("style");
      el.setAttribute("data-tv-ui", "");
      el.textContent = text;
      (doc.head || doc.documentElement).appendChild(el);
      return { el: el };
    }
  }
  function setSheetText(holder, text) {
    if (holder.sheet) { holder.sheet.replaceSync(text); } else { holder.el.textContent = text; }
  }

  function attachment() { return state.mode === "attachment"; }

  function isDark() {
    // An attachment keeps its own colours: highlight variants follow what the page shows.
    if (attachment()) {
      const bg = pageBackground();
      const m = bg && /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(bg);
      return m ? (0.2126 * m[1] + 0.7152 * m[2] + 0.0722 * m[3]) / 255 < 0.4 : false;
    }
    if (state.theme === "dark") { return true; }
    if (state.theme === "light") { return false; }
    return root.matchMedia && root.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function styleFor(name) {
    const base = state.styles[name] || DEFAULT_STYLES[name] || DEFAULT_STYLES.card;
    return isDark() && base.dark ? Object.assign({}, base, base.dark) : base;
  }

  function cssColor(value) {
    // Only colour tokens: no "url(", ";" or braces from app data into a stylesheet.
    return typeof value === "string" && /^[#a-zA-Z0-9(),.%\s-]+$/.test(value) ? value : null;
  }

  function highlightApi() {
    return !!(root.CSS && root.CSS.highlights && typeof root.Highlight === "function") && !state.options.noHighlightApi;
  }

  function rebuildDynamicSheet() {
    const rules = [];
    if (state.textPercent !== 100 && state.baseFontPx > 0) {
      rules.push("body { font-size: " + (state.baseFontPx * state.textPercent / 100).toFixed(2) + "px !important; }");
    }
    const names = Object.keys(Object.assign({}, DEFAULT_STYLES, state.styles));
    names.forEach(function (name) {
      if (!/^[a-z0-9-]+$/.test(name)) { return; }
      const s = styleFor(name);
      const bg = cssColor(s.background);
      const fg = cssColor(s.color);
      const underline = cssColor(s.underline);
      const decl = [];
      if (bg) { decl.push("background-color: " + bg); }
      if (fg) { decl.push("color: " + fg); }
      if (s.strike) { decl.push("text-decoration: line-through"); }
      else if (underline) { decl.push("text-decoration: underline 2px " + underline); }
      if (highlightApi()) { rules.push("::highlight(tv-" + name + ") { " + decl.join("; ") + "; }"); }
      if (bg) { rules.push(".tv-hl.tv-hl-" + name + " { background-color: " + bg + "; }"); }
      if (underline || s.strike) { rules.push(".tv-hl.tv-hl-" + name + " { border-bottom: 2px solid " + (underline || "currentColor") + "; }"); }
      rules.push("span.tm.tv-point.tv-point-" + name + "::before { color: " + (underline || bg || "currentColor").replace(/rgba\(([^,]+),([^,]+),([^,]+),[^)]+\)/, "rgb($1,$2,$3)") + "; }");
    });
    const mask = maskColor();
    rules.push(".tv-mask-block { background-color: " + mask + "; }");
    rules.push(".tv-mask-inverted path { fill: " + mask + "; }");
    if (!state.dynSheet) { state.dynSheet = makeSheet(rules.join("\n")); } else { setSheetText(state.dynSheet, rules.join("\n")); }
  }

  // The mask is the page background: a hidden answer reads as blank paper (desktop and Android).
  function maskColor() {
    if (state.options.maskColor && cssColor(state.options.maskColor)) { return state.options.maskColor; }
    return pageBackground() || (isDark() ? "#151515" : "#ffffff");
  }
  // The page's own background colour (body, else html), null when both are transparent.
  function pageBackground() {
    const transparent = function (c) { return !c || c === "transparent" || /rgba\([^)]*,\s*0\)$/.test(c); };
    let c = doc.body ? root.getComputedStyle(doc.body).backgroundColor : "";
    if (transparent(c)) { c = root.getComputedStyle(doc.documentElement).backgroundColor; }
    return transparent(c) ? null : c;
  }

  // ---- anchors and the text index ----------------------------------------------

  function isUi(node) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return !!(el && el.closest && el.closest("[data-tv-ui]"));
  }

  function scanAnchors() {
    state.anchors = new Map();
    state.anchorList = [];
    state.warnings = [];
    const elements = doc.querySelectorAll("[data-tm][data-key]");
    for (let i = 0; i < elements.length; ++i) {
      const el = elements[i];
      if (isUi(el)) { continue; }
      const kind = el.getAttribute("data-tm");
      const key = el.getAttribute("data-key");
      if (kind !== "start" && kind !== "end" && kind !== "point") { continue; }
      let entry = state.anchors.get(key);
      if (!entry) { entry = {}; state.anchors.set(key, entry); }
      if (entry[kind]) { state.warnings.push("duplicate " + kind + " anchor " + key); continue; }
      entry[kind] = el;
      state.anchorList.push(el);
    }
    state.anchors.forEach(function (entry, key) {
      if (!!entry.start !== !!entry.end) { state.warnings.push("unpaired anchor " + key); }
    });
  }

  function anchorRange(key) {
    const entry = state.anchors.get(key);
    if (!entry || !entry.start || !entry.end) { return null; }
    // The end must follow the start.
    if (!(entry.start.compareDocumentPosition(entry.end) & 4 /* FOLLOWING */)) { return null; }
    const r = doc.createRange();
    r.setStartAfter(entry.start);
    r.setEndBefore(entry.end);
    return r;
  }

  function index() {
    // A page that changed its DOM since the last index: rebuild everything first (attachment mode).
    if (state.mutationObserver && !state.flushing && pendingMutations()) { flushReindex(); }
    if (!state.index) { state.index = new T.TextIndex(doc.body); }
    return state.index;
  }

  // ---- geometry helpers ------------------------------------------------------------

  function clientRect(r) { return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }; }

  // Visible part of an element's content: clipped by overflow ancestors (scrolling tables, displays).
  function clipFor(el) {
    if (!state.layoutClip) { state.layoutClip = new Map(); }
    const cache = state.layoutClip;
    if (!el || el === doc.body || el === doc.documentElement) { return null; }
    if (cache.has(el)) { return cache.get(el); }
    let clip = clipFor(el.parentElement);
    const cs = root.getComputedStyle(el);
    if (cs.overflowX !== "visible" || cs.overflowY !== "visible") {
      const b = el.getBoundingClientRect();
      const own = { left: b.left, top: b.top, right: b.right, bottom: b.bottom };
      clip = clip ? intersect(clip, own) : own;
    }
    cache.set(el, clip);
    return clip;
  }
  function intersect(a, b) {
    return { left: Math.max(a.left, b.left), top: Math.max(a.top, b.top), right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom) };
  }
  function clipped(rect, el) {
    const c = clipFor(el);
    if (!c) { return rect; }
    const r = intersect(rect, c);
    return r.right - r.left > 0.5 && r.bottom - r.top > 0.5 ? r : null;
  }

  // Text and object fragments of a Range in viewport coordinates (the "words" the PDF
  // apps get from the page text): one rect per line box of each text node part, and
  // the box of each atomic element (formula SVG, image).
  function rangeFragments(range) {
    const out = [];
    const ca = range.commonAncestorContainer;
    const addText = function (node, s, e) {
      if (e <= s || /^\s*$/.test(node.data.slice(s, e))) { return; }
      const r = doc.createRange();
      r.setStart(node, s);
      r.setEnd(node, e);
      const rects = r.getClientRects();
      for (let i = 0; i < rects.length; ++i) {
        const q = rects[i];
        if (q.width > 0.5 && q.height > 0.5) {
          const c = clipped(clientRect(q), node.parentElement);
          if (c) { out.push(c); }
        }
      }
    };
    if (ca.nodeType === 3) {
      addText(ca, range.startOffset, range.endOffset);
      return out;
    }
    const addBox = function (el) {
      const b = el.getBoundingClientRect();
      if (b.width > 0.5 && b.height > 0.5) {
        const c = clipped(clientRect(b), el.parentElement);
        if (c) { out.push(c); }
      }
    };
    const walker = doc.createTreeWalker(ca, 1 | 4, {
      acceptNode: function (n) {
        if (n.nodeType === 1) {
          if (n.hasAttribute("data-tv-ui") || n.hasAttribute("data-tm")) { return 2; }
          // An object (formula SVG, image) is one box; its insides are not walked.
          if (ATOMIC.has(n.localName)) {
            if (range.intersectsNode(n)) { addBox(n); }
            return 2;
          }
          return T.skipElement(n) ? 2 : 3;
        }
        return 1;
      }
    });
    let n;
    while ((n = walker.nextNode())) {
      if (range.comparePoint(n, n.data.length) < 0) { continue; } // before the start
      if (range.comparePoint(n, 0) > 0) { break; }                // after the end
      addText(n, n === range.startContainer ? range.startOffset : 0, n === range.endContainer ? range.endOffset : n.data.length);
    }
    return out;
  }

  function scroll() { return { x: root.scrollX || 0, y: root.scrollY || 0 }; }

  function toDoc(r) {
    const s = scroll();
    return { left: r.left + s.x, top: r.top + s.y, right: r.right + s.x, bottom: r.bottom + s.y };
  }

  // The text column: the PDF's page width analogue for the geometry constants.
  function referenceWidth() {
    const b = doc.body.getBoundingClientRect();
    const w = b.width || doc.documentElement.clientWidth || root.innerWidth;
    return Math.max(100, w);
  }

  // Every "word" (text box and object box) in a vertical band of the document, in
  // document coordinates: the page text the mask rules check against.
  function wordsInBand(top, bottom) {
    const idx = index();
    const out = [];
    const s = scroll();
    const r = doc.createRange();
    const word = /\S+/g;
    for (let i = 0; i < idx.nodes.length; ++i) {
      const node = idx.nodes[i];
      r.selectNodeContents(node);
      const b = r.getBoundingClientRect();
      if (b.bottom + s.y < top || b.top + s.y > bottom || (b.width === 0 && b.height === 0)) { continue; }
      word.lastIndex = 0;
      let m;
      while ((m = word.exec(node.data))) {
        r.setStart(node, m.index);
        r.setEnd(node, m.index + m[0].length);
        const rects = r.getClientRects();
        for (let k = 0; k < rects.length; ++k) {
          const q = rects[k];
          if (q.width <= 0.5 || q.height <= 0.5) { continue; }
          const c = clipped(clientRect(q), node.parentElement);
          if (c && c.bottom + s.y >= top && c.top + s.y <= bottom) { out.push(toDoc(c)); }
        }
      }
    }
    const objects = doc.body.querySelectorAll("svg, img, video, canvas, math, picture, iframe, object, embed");
    for (let i = 0; i < objects.length; ++i) {
      const el = objects[i];
      if (isUi(el) || (el.parentElement && el.parentElement.closest("svg, math, picture"))) { continue; }
      const b = el.getBoundingClientRect();
      if (b.width <= 0.5 || b.height <= 0.5) { continue; }
      const c = clipped(clientRect(b), el.parentElement);
      if (c && c.bottom + s.y >= top && c.top + s.y <= bottom) { out.push(toDoc(c)); }
    }
    return out;
  }

  function median(values) {
    const v = values.slice().sort(function (a, b) { return a - b; });
    return v.length ? v[Math.floor(v.length / 2)] : 0;
  }

  // Review mask blocks (document coordinates) of a set of answer ranges.
  function maskBlocks(ranges) {
    state.layoutClip = null;
    let fragments = [];
    ranges.forEach(function (range) { fragments = fragments.concat(rangeFragments(range).map(toDoc)); });
    if (fragments.length === 0) { return []; }
    const lineHeight = median(fragments.map(function (f) { return f.bottom - f.top; }));
    let top = Infinity, bottom = -Infinity;
    fragments.forEach(function (f) { top = Math.min(top, f.top); bottom = Math.max(bottom, f.bottom); });
    const words = wordsInBand(top - 4 * lineHeight, bottom + 4 * lineHeight);
    const W = referenceWidth();
    const n = function (r) { return { left: r.left / W, top: r.top / W, right: r.right / W, bottom: r.bottom / W }; };
    const de = doc.documentElement;
    const bounds = { right: Math.max(de.scrollWidth, root.innerWidth) / W, bottom: Math.max(de.scrollHeight, root.innerHeight) / W };
    const blocks = G.continuousReviewMaskBlocks(fragments.map(n), 1, words.map(n), bounds);
    return G.sortBlocks(blocks.map(function (b) {
      return { left: b.left * W, top: b.top * W, right: b.right * W, bottom: b.bottom * W };
    }));
  }

  // ---- overlay layer -----------------------------------------------------------

  function ensureLayer() {
    if (state.layer && state.layer.isConnected) { return; }
    const layer = doc.createElement("div");
    layer.setAttribute("data-tv-ui", "");
    layer.className = "tv-layer";
    const hl = doc.createElement("div");
    hl.className = "tv-hl-layer";
    const mask = doc.createElement("div");
    mask.className = "tv-mask-layer";
    layer.appendChild(hl);
    layer.appendChild(mask);
    doc.documentElement.appendChild(layer);
    state.layer = layer;
    state.hlLayer = hl;
    state.maskLayer = mask;
  }

  // Document coordinates → the layer's own coordinates.
  function layerOrigin() {
    const b = state.layer.getBoundingClientRect();
    const s = scroll();
    return { x: b.left + s.x, y: b.top + s.y };
  }

  function placeBox(el, r, o) {
    el.style.left = (r.left - o.x) + "px";
    el.style.top = (r.top - o.y) + "px";
    el.style.width = Math.max(0, r.right - r.left) + "px";
    el.style.height = Math.max(0, r.bottom - r.top) + "px";
  }

  // ---- items (cards, comments, feedback) -------------------------------------------

  function itemKey(kind, id) { return kind + "\n" + id; }

  function asList(v) { return Array.isArray(v) ? v : []; }

  function feedbackStyle(fb) {
    return fb.polarity === "negative" ? "feedback-negative" : fb.polarity === "neutral" ? "feedback-neutral" : "feedback-positive";
  }

  function quoteRange(quote, mode) {
    const idx = index();
    if (!idx.cache) { idx.cache = {}; }
    const hit = T.resolveQuote(idx.text, quote, mode, idx.cache);
    return hit ? idx.rangeOf(hit.start, hit.end) : null;
  }

  // Answer text of a card (its segments joined by "\n") with a map back to the index.
  function segmentsHaystack(ranges) {
    const idx = index();
    const parts = [];
    const map = [];
    ranges.forEach(function (range, i) {
      const span = idx.spanOfRange(range);
      if (i > 0) { parts.push("\n"); map.push(-1); }
      parts.push(idx.text.slice(span[0], span[1]));
      for (let k = span[0]; k < span[1]; ++k) { map.push(k); }
    });
    return { text: parts.join(""), map: map };
  }

  function placeAnchors(keys) {
    const ranges = [], points = [], missing = [];
    asList(keys).forEach(function (key) {
      if (typeof key !== "string") { return; }
      const range = anchorRange(key);
      if (range) { ranges.push({ key: key, range: range }); return; }
      const entry = state.anchors.get(key);
      if (entry && entry.point) { points.push({ key: key, el: entry.point }); return; }
      missing.push(key);
    });
    return { ranges: ranges, points: points, missing: missing };
  }

  function clearPointMarks() {
    state.items.forEach(function (p) {
      p.points.forEach(function (pt) {
        pt.el.classList.remove("tv-point");
        Array.from(pt.el.classList).forEach(function (c) { if (c.indexOf("tv-point-") === 0) { pt.el.classList.remove(c); } });
        pt.el.removeAttribute("data-tv-item");
        pt.el.removeAttribute("data-tv-kind");
      });
    });
  }

  function setItems(cards, comments, feedback, options) {
    options = options || {};
    if (options.styles && typeof options.styles === "object") {
      state.styles = Object.assign({}, DEFAULT_STYLES);
      Object.keys(options.styles).forEach(function (k) { state.styles[k] = options.styles[k]; });
    }
    if (options.highlight_cards !== undefined) { state.highlightCards = !!options.highlight_cards; }
    if (options.show_feedback) { state.feedbackMode = options.show_feedback; }
    state.itemInputs = { cards: asList(cards), comments: asList(comments), feedback: asList(feedback) };
    const report = placeItems();
    state.lastReportJson = JSON.stringify(report);
    rebuildDynamicSheet();
    renderHighlights();
    return report;
  }

  // Places the items of the last setItems in the current DOM (again after DOM changes).
  function placeItems() {
    const inputs = state.itemInputs || { cards: [], comments: [], feedback: [] };
    const cards = inputs.cards, comments = inputs.comments, feedback = inputs.feedback;
    clearPointMarks();
    state.items = new Map();
    const report = { cards: { placed: [], partial: [], unplaced: [] }, comments: { placed: [], partial: [], unplaced: [] },
      feedback: { placed: [], unplaced: [] } };

    asList(cards).forEach(function (card) {
      if (!card || typeof card.id !== "string") { return; }
      const answer = placeAnchors(card.anchors);
      const question = placeAnchors(card.question_anchors);
      const p = { kind: "card", id: card.id, style: card.selected ? "card-selected" : (card.style || "card"),
        ranges: answer.ranges.map(function (r) { return r.range; }), keys: answer.ranges.map(function (r) { return r.key; }),
        points: answer.points, question: question.ranges.map(function (r) { return r.range; }), via: "anchors",
        missing: answer.missing };
      if (p.ranges.length === 0 && p.points.length === 0 && card.text_quote) {
        const r = quoteRange(card.text_quote, "best");
        // An edition's quote card waits for its markers ("pending"); an attachment's cards are all quote cards.
        if (r) { p.ranges = [r]; p.via = "quote"; p.style = card.selected ? "card-selected" : (card.style || (attachment() ? "card" : "pending")); }
      }
      if (p.ranges.length === 0 && p.points.length === 0) {
        report.cards.unplaced.push({ id: card.id, reason: card.text_quote && !asList(card.anchors).length ? "quote_not_found" : "no_anchor_in_html",
          missing: answer.missing });
        return;
      }
      state.items.set(itemKey("card", card.id), p);
      if (answer.missing.length) { report.cards.partial.push({ id: card.id, missing: answer.missing }); }
      report.cards.placed.push(card.id);
    });

    asList(comments).forEach(function (comment) {
      if (!comment || typeof comment.id !== "string") { return; }
      const placed = placeAnchors(comment.anchors);
      const strike = comment.style === "comment-strike" || comment.strike === true;
      const p = { kind: "comment", id: comment.id, style: comment.selected ? "comment-selected" : (comment.style || (strike ? "comment-strike" : "comment")),
        ranges: placed.ranges.map(function (r) { return r.range; }), keys: placed.ranges.map(function (r) { return r.key; }),
        points: placed.points, question: [], via: "anchors", missing: placed.missing };
      if (p.ranges.length === 0 && p.points.length === 0) {
        // pdf_only comments: by their exact selected text when it occurs once.
        let r = null;
        if (typeof comment.selected_text === "string" && comment.selected_text.trim()) {
          r = quoteRange({ exact: comment.selected_text }, "unique");
          if (r) { p.via = "selected_text"; }
        }
        if (!r && comment.text_quote) {
          r = quoteRange(comment.text_quote, "best");
          if (r) { p.via = "quote"; }
        }
        if (r) { p.ranges = [r]; }
      }
      if (p.ranges.length === 0 && p.points.length === 0) {
        report.comments.unplaced.push({ id: comment.id, reason: comment.anchors && comment.anchors.length ? "no_anchor_in_html" :
          (comment.selected_text ? "selected_text_not_unique_or_missing" : "no_position"), missing: placed.missing });
        return;
      }
      state.items.set(itemKey("comment", comment.id), p);
      if (placed.missing.length) { report.comments.partial.push({ id: comment.id, missing: placed.missing }); }
      report.comments.placed.push(comment.id);
    });

    asList(feedback).forEach(function (fb) {
      if (!fb || typeof fb.id !== "string") { return; }
      const quote = fb.text_quote || fb.quote;
      // Document feedback of an attachment has no card: its quote is found in the whole page.
      if (!fb.card_id && attachment()) {
        const found = quote ? quoteRange(quote, "best") : null;
        if (!found) { report.feedback.unplaced.push({ id: fb.id, reason: quote ? "orphaned" : "no_quote" }); return; }
        state.items.set(itemKey("feedback", fb.id), { kind: "feedback", id: fb.id,
          style: feedbackStyle(fb), ranges: [found], keys: [], points: [],
          question: [], via: "quote", missing: [] });
        report.feedback.placed.push(fb.id);
        return;
      }
      const card = state.items.get(itemKey("card", fb.card_id));
      if (!card || card.ranges.length === 0 || !quote) {
        report.feedback.unplaced.push({ id: fb.id, reason: !card ? "card_not_shown" : "no_quote" });
        return;
      }
      const hay = segmentsHaystack(card.ranges);
      const hit = T.resolveQuote(hay.text, quote, "best");
      let range = null;
      if (hit) {
        let s = hit.start, e = hit.end;
        while (s < e && hay.map[s] < 0) { ++s; }
        while (e > s && hay.map[e - 1] < 0) { --e; }
        if (e > s) { range = index().rangeOf(hay.map[s], hay.map[e - 1] + 1); }
      }
      if (!range) { report.feedback.unplaced.push({ id: fb.id, reason: "orphaned" }); return; }
      state.items.set(itemKey("feedback", fb.id), { kind: "feedback", id: fb.id, cardId: fb.card_id,
        style: feedbackStyle(fb), ranges: [range], keys: [], points: [],
        question: [], via: "quote", missing: [] });
      report.feedback.placed.push(fb.id);
    });

    state.items.forEach(function (p) {
      p.points.forEach(function (pt) {
        pt.el.classList.add("tv-point", "tv-point-" + p.style);
        pt.el.setAttribute("data-tv-item", p.id);
        pt.el.setAttribute("data-tv-kind", p.kind);
      });
    });
    return report;
  }

  function feedbackVisible(p) {
    if (state.feedbackMode === "always") { return true; }
    if (state.feedbackMode === "never") { return false; }
    return !!(state.review && state.review.revealed && state.review.cardId === p.cardId);
  }

  function highlightGroups() {
    const groups = {};
    const add = function (name, range) { (groups[name] = groups[name] || []).push(range); };
    state.items.forEach(function (p) {
      if (p.kind === "card" && !state.highlightCards && p.style === "card") { return; }
      if (p.kind === "feedback" && !feedbackVisible(p)) { return; }
      p.ranges.forEach(function (r) { add(p.style, r); });
    });
    if (state.review) { state.review.question.forEach(function (r) { add("question", r); }); }
    return groups;
  }

  function renderHighlights() {
    const groups = highlightGroups();
    if (highlightApi()) {
      const reg = root.CSS.highlights;
      Array.from(reg.keys()).forEach(function (k) { if (k.indexOf("tv-") === 0) { reg.delete(k); } });
      Object.keys(groups).forEach(function (name) {
        const h = new root.Highlight();
        groups[name].forEach(function (r) { h.add(r); });
        const pr = STYLE_PRIORITY.indexOf(name);
        h.priority = pr < 0 ? STYLE_PRIORITY.length : pr;
        reg.set("tv-" + name, h);
      });
      if (state.hlLayer) { state.hlLayer.textContent = ""; }
      return;
    }
    // Fallback: line boxes as overlay elements (recomputed on layout changes).
    ensureLayer();
    state.layoutClip = null;
    const o = layerOrigin();
    const frag = doc.createDocumentFragment();
    STYLE_PRIORITY.concat(Object.keys(groups).filter(function (n) { return STYLE_PRIORITY.indexOf(n) < 0; })).forEach(function (name) {
      (groups[name] || []).forEach(function (range) {
        G.mergeTextLineRects(rangeFragments(range).map(toDoc)).forEach(function (line) {
          const d = doc.createElement("div");
          d.className = "tv-hl tv-hl-" + name;
          placeBox(d, line, o);
          frag.appendChild(d);
        });
      });
    });
    state.hlLayer.textContent = "";
    state.hlLayer.appendChild(frag);
  }

  // Items under a viewport point, most specific first.
  function itemsAtPoint(x, y, target) {
    if (state.mutationObserver && pendingMutations()) { flushReindex(); }
    const hits = [];
    const seen = new Set();
    const pointEl = target && target.closest ? target.closest("[data-tv-item]") : null;
    if (pointEl) {
      const kind = pointEl.getAttribute("data-tv-kind");
      const id = pointEl.getAttribute("data-tv-item");
      hits.push({ id: id, kind: kind, area: 0 });
      seen.add(itemKey(kind, id));
    }
    const kindOrder = { feedback: 0, comment: 1, card: 2 };
    state.items.forEach(function (p, key) {
      if (seen.has(key)) { return; }
      if (p.kind === "feedback" && !feedbackVisible(p)) { return; }
      if (p.kind === "card" && state.review && !state.review.revealed && state.review.cardId === p.id) { return; }
      let area = 0, hit = false;
      p.ranges.forEach(function (range) {
        const rects = range.getClientRects();
        for (let i = 0; i < rects.length; ++i) {
          const q = rects[i];
          if (q.width <= 0 || q.height <= 0) { continue; }
          area += q.width * q.height;
          if (x >= q.left - 1 && x <= q.right + 1 && y >= q.top - 1 && y <= q.bottom + 1) { hit = true; }
        }
      });
      if (hit) { hits.push({ id: p.id, kind: p.kind, area: area }); }
    });
    if (state.review) {
      state.review.question.forEach(function (range) {
        const rects = range.getClientRects();
        for (let i = 0; i < rects.length; ++i) {
          const q = rects[i];
          if (x >= q.left && x <= q.right && y >= q.top && y <= q.bottom) {
            hits.push({ id: state.review.cardId, kind: "question", area: 1e12 });
            return;
          }
        }
      });
    }
    hits.sort(function (a, b) {
      const ka = kindOrder[a.kind] === undefined ? 3 : kindOrder[a.kind];
      const kb = kindOrder[b.kind] === undefined ? 3 : kindOrder[b.kind];
      return ka !== kb ? ka - kb : a.area - b.area;
    });
    return hits.map(function (h) { return { id: h.id, kind: h.kind }; });
  }

  // ---- review mask -------------------------------------------------------------------

  function rangeContainsNode(range, node) {
    const r = doc.createRange();
    r.selectNode(node);
    return range.compareBoundaryPoints(Range.START_TO_START, r) <= 0 && range.compareBoundaryPoints(Range.END_TO_END, r) >= 0;
  }

  // Elements entirely inside the answer: not selectable (and invisible while hidden).
  function containedElements(range) {
    const out = [];
    const ca = range.commonAncestorContainer;
    if (ca.nodeType !== 1) { return out; }
    const walker = doc.createTreeWalker(ca, 1, null);
    let n = walker.nextNode();
    while (n) {
      if (n.hasAttribute("data-tv-ui")) { n = nextSkippingChildren(walker, n, ca); continue; }
      if (rangeContainsNode(range, n)) {
        if (!n.hasAttribute("data-tm")) { out.push(n); }
        n = nextSkippingChildren(walker, n, ca);
        continue;
      }
      if (!range.intersectsNode(n) && range.comparePoint(n, 0) > 0) { break; }
      n = walker.nextNode();
    }
    return out;
  }
  function nextSkippingChildren(walker, n, rootNode) {
    let cur = n;
    while (cur && cur !== rootNode) {
      if (cur.nextElementSibling) { walker.currentNode = cur.nextElementSibling; return cur.nextElementSibling; }
      cur = cur.parentElement;
    }
    return null;
  }

  // The answer (and question) ranges of a review in the current DOM.
  function resolveReview(review) {
    const opts = review.opts;
    if (Array.isArray(opts.anchors)) {
      return { ranges: placeAnchors(opts.anchors).ranges.map(function (r) { return r.range; }),
        question: placeAnchors(opts.question_anchors).ranges.map(function (r) { return r.range; }) };
    }
    if (opts.text_quote) {
      const r = quoteRange(opts.text_quote, "best");
      return { ranges: r ? [r] : [], question: [] };
    }
    const p = state.items.get(itemKey("card", review.cardId));
    return p ? { ranges: p.ranges, question: p.question } : { ranges: [], question: [] };
  }

  function cardInput(cardId) {
    const cards = state.itemInputs ? state.itemInputs.cards : [];
    for (const c of cards) { if (c && c.id === cardId) { return c; } }
    return null;
  }

  function presentReview(cardId, opts) {
    opts = opts || {};
    clearReview(true);
    const review = { cardId: cardId, opts: opts, ranges: [], question: [], inverted: !!opts.inverted, revealed: false,
      blocks: [], contained: [], pending: false, found: false, timer: 0 };
    const viaItem = !Array.isArray(opts.anchors) && !opts.text_quote;
    if (viaItem && !state.items.get(itemKey("card", cardId))) {
      // Attachment pages render text late: a quote card not placed yet is waited for.
      const input = attachment() ? cardInput(cardId) : null;
      if (!input || !input.text_quote) { throw new Error("card not shown in the HTML view: " + cardId); }
    }
    const res = resolveReview(review);
    review.ranges = res.ranges;
    review.question = res.question;
    if (review.ranges.length === 0 && !attachment()) { throw new Error("card has no answer passage in the HTML view: " + cardId); }
    if (!answerVisible(review)) {
      // Not in the page yet (or in a hidden tab): wait for the page to render it.
      review.pending = true;
      state.review = review;
      waitForAnswer(review);
      return { blocks: 0, pending: true };
    }
    state.review = review;
    activateReview(review);
    return { blocks: review.blocks.length };
  }

  function activateReview(review) {
    review.pending = false;
    review.found = true;
    if (review.timer) { root.clearTimeout(review.timer); review.timer = 0; }
    markContained(review);
    doc.documentElement.classList.add("tv-reviewing");
    applyMaskState();
    layoutReview(true);
    renderHighlights();
    clearSelectionIfMasked();
    if (review.opts.scroll !== false && review.blocks.length) { focusBlock(0); }
    sendMaskState();
  }

  // Attachment mode: an answer counts as found when it has something to hide on screen.
  function answerVisible(review) {
    if (review.ranges.length === 0) { return false; }
    return !attachment() || maskBlocks(review.ranges).length > 0;
  }

  function markContained(review) {
    review.contained.forEach(function (el) { el.classList.remove("tv-masked"); });
    review.contained = [];
    if (review.revealed) { return; }
    review.ranges.forEach(function (r) { review.contained = review.contained.concat(containedElements(r)); });
    review.contained.forEach(function (el) { el.classList.add("tv-masked"); });
  }

  // A pending review: retried after every reindex; "not found" once the page has settled
  // and the answer still is not there after reviewTimeoutMs.
  function waitForAnswer(review) {
    const timeout = state.options.reviewTimeoutMs === undefined ? 3000 : Number(state.options.reviewTimeoutMs);
    settle(settleTimeout()).then(guarded("review-wait", function () {
      if (state.review !== review || !review.pending) { return; }
      review.timer = root.setTimeout(guarded("review-timeout", function () {
        review.timer = 0;
        if (state.review !== review || !review.pending) { return; }
        if (pendingMutations()) { flushReindex(); }
        if (state.review !== review || !review.pending) { return; }
        state.review = null;
        send("reviewMask", { card_id: review.cardId, state: "not_found", found: false, inverted: review.inverted, blocks: 0 });
      }), timeout);
    }));
  }

  // After a DOM change: the answer again from the new DOM, mask and masked elements recomputed.
  function refreshReview() {
    const r = state.review;
    if (!r) { return; }
    const res = resolveReview(r);
    if (r.pending) {
      r.ranges = res.ranges;
      r.question = res.question;
      if (answerVisible(r)) {
        if (r.revealed) {
          // Revealed while waiting: frames around the answer once it is there.
          r.pending = false;
          r.found = true;
          layoutReview(true);
          sendMaskState();
        } else { activateReview(r); }
      } else {
        r.ranges = [];
        r.question = [];
      }
      return;
    }
    r.ranges = res.ranges;
    r.question = res.question;
    markContained(r);
    layoutReview(false);
  }

  function applyMaskState() {
    const de = doc.documentElement;
    const r = state.review;
    const on = !!(r && !r.revealed && !r.pending);
    de.classList.toggle("tv-mask-hidden", on && !r.inverted);
    de.classList.toggle("tv-mask-inverted-on", on && r.inverted);
  }

  function layoutReview(force) {
    const r = state.review;
    if (!r || r.pending) { return; }
    const before = r.blocks.length;
    r.blocks = maskBlocks(r.ranges);
    r.found = r.blocks.length > 0;
    renderMask();
    if (!force && before !== r.blocks.length) { sendMaskState(); }
  }

  function renderMask() {
    ensureLayer();
    const layer = state.maskLayer;
    layer.textContent = "";
    const r = state.review;
    if (!r || r.pending) { return; }
    const o = layerOrigin();
    if (r.revealed) {
      // A revealed answer keeps the exact area that was hidden, framed so it is easy to find.
      r.blocks.forEach(function (b, i) {
        const d = doc.createElement("div");
        d.className = "tv-mask-frame";
        d.setAttribute("data-index", String(i));
        placeBox(d, { left: b.left - 5, top: b.top - 4.5, right: b.right + 5, bottom: b.bottom + 4.5 }, o);
        layer.appendChild(d);
      });
      return;
    }
    if (r.inverted) {
      // Flipped: the answer stays readable and its context is hidden.
      const de = doc.documentElement;
      const w = Math.max(de.scrollWidth, root.innerWidth), h = Math.max(de.scrollHeight, root.innerHeight);
      const ns = "http://www.w3.org/2000/svg";
      const svg = doc.createElementNS(ns, "svg");
      svg.setAttribute("class", "tv-mask-inverted");
      svg.setAttribute("width", String(w));
      svg.setAttribute("height", String(h));
      svg.style.left = (-o.x) + "px";
      svg.style.top = (-o.y) + "px";
      let d = "M0 0H" + w + "V" + h + "H0Z";
      r.blocks.forEach(function (b) {
        d += "M" + (b.left - 2) + " " + (b.top - 1.5) + "H" + (b.right + 2) + "V" + (b.bottom + 1.5) + "H" + (b.left - 2) + "Z";
      });
      const path = doc.createElementNS(ns, "path");
      path.setAttribute("d", d);
      path.setAttribute("fill-rule", "evenodd");
      svg.appendChild(path);
      layer.appendChild(svg);
      return;
    }
    r.blocks.forEach(function (b, i) {
      const d = doc.createElement("div");
      d.className = "tv-mask-block";
      d.setAttribute("data-index", String(i));
      placeBox(d, { left: b.left - 2, top: b.top - 1.5, right: b.right + 2, bottom: b.bottom + 1.5 }, o);
      layer.appendChild(d);
    });
  }

  function sendMaskState() {
    const r = state.review;
    const msg = r ? { card_id: r.cardId, state: r.revealed ? "revealed" : (r.pending ? "pending" : "hidden"), inverted: r.inverted,
      blocks: r.blocks.length }
      : { card_id: null, state: "none", blocks: 0 };
    // Attachments: whether the answer is in the page now (it can leave with a tab switch).
    if (attachment() && r) { msg.found = !r.pending && r.blocks.length > 0; }
    send("reviewMask", msg);
  }

  function reveal() {
    const r = state.review;
    if (!r) { return { blocks: 0 }; }
    if (!r.revealed) {
      r.revealed = true;
      r.inverted = false;
      if (r.timer) { root.clearTimeout(r.timer); r.timer = 0; }
      r.contained.forEach(function (el) { el.classList.remove("tv-masked"); });
      applyMaskState();
      renderMask();
      renderHighlights();
      sendMaskState();
    }
    return { blocks: r.blocks.length };
  }

  function setReviewInverted(inverted) {
    const r = state.review;
    if (!r || r.revealed) { return false; }
    if (r.inverted !== !!inverted) {
      r.inverted = !!inverted;
      applyMaskState();
      renderMask();
      sendMaskState();
    }
    return true;
  }

  function clearReview(silent) {
    const r = state.review;
    if (r) {
      r.contained.forEach(function (el) { el.classList.remove("tv-masked"); });
      if (r.timer) { root.clearTimeout(r.timer); r.timer = 0; }
    }
    state.review = null;
    doc.documentElement.classList.remove("tv-reviewing");
    applyMaskState();
    if (state.maskLayer) { state.maskLayer.textContent = ""; }
    if (r) {
      renderHighlights();
      if (silent !== true) { sendMaskState(); }
    }
    return true;
  }

  function focusBlock(i) {
    const r = state.review;
    if (!r || i < 0 || i >= r.blocks.length) { return false; }
    root.scrollTo(root.scrollX, Math.max(0, r.blocks[i].top - FOCUS_MARGIN * root.innerHeight));
    return true;
  }

  // Selections never take masked text: a drag over the hidden answer stops at its edge.
  function maskRangesActive() {
    const r = state.review;
    return r && !r.revealed && !r.inverted ? r.ranges : null;
  }
  function cmp(nodeA, offA, nodeB, offB) {
    const a = doc.createRange();
    a.setStart(nodeA, offA);
    const b = doc.createRange();
    b.setStart(nodeB, offB);
    return a.compareBoundaryPoints(Range.START_TO_START, b);
  }
  function clipSelectionToMask() {
    const masks = maskRangesActive();
    const sel = root.getSelection();
    if (!masks || !sel || sel.rangeCount === 0 || sel.isCollapsed) { return; }
    for (let guard = 0; guard < 8; ++guard) {
      const range = sel.getRangeAt(0);
      let changed = false;
      for (const m of masks) {
        const overlaps = cmp(range.endContainer, range.endOffset, m.startContainer, m.startOffset) > 0 &&
          cmp(range.startContainer, range.startOffset, m.endContainer, m.endOffset) < 0;
        if (!overlaps) { continue; }
        const a = cmp(sel.anchorNode, sel.anchorOffset, m.startContainer, m.startOffset);
        const b = cmp(sel.anchorNode, sel.anchorOffset, m.endContainer, m.endOffset);
        if (a > 0 && b < 0) { sel.removeAllRanges(); return; }
        if (a <= 0) { sel.extend(m.startContainer, m.startOffset); } else { sel.extend(m.endContainer, m.endOffset); }
        changed = true;
        break;
      }
      if (!changed || sel.rangeCount === 0 || sel.isCollapsed) { return; }
    }
  }
  function clearSelectionIfMasked() {
    const masks = maskRangesActive();
    const sel = root.getSelection();
    if (!masks || !sel || sel.rangeCount === 0 || sel.isCollapsed) { return; }
    const range = sel.getRangeAt(0);
    for (const m of masks) {
      if (cmp(range.endContainer, range.endOffset, m.startContainer, m.startOffset) > 0 &&
          cmp(range.startContainer, range.startOffset, m.endContainer, m.endOffset) < 0) { sel.removeAllRanges(); return; }
    }
  }

  // ---- selection ---------------------------------------------------------------------

  function srcList(attr) {
    if (!state.srcLists) { state.srcLists = {}; }
    if (!state.srcLists[attr]) {
      const els = doc.querySelectorAll("[" + attr + "]");
      const list = [];
      for (let i = 0; i < els.length; ++i) {
        if (isUi(els[i])) { continue; }
        const parsed = T.parseSrc(els[i].getAttribute(attr));
        if (parsed) { parsed.el = els[i]; parsed.ordinal = list.length; list.push(parsed); }
      }
      state.srcLists[attr] = list;
    }
    return state.srcLists[attr];
  }

  function hasText(el) { return /\S/.test(T.elementText(el)) || !!el.querySelector("svg, img, math"); }

  // First and last innermost elements carrying `attr` that the range touches.
  function boundaryElements(range, attr, needText) {
    let ca = range.commonAncestorContainer;
    if (ca.nodeType !== 1) { ca = ca.parentElement; }
    const inside = Array.prototype.filter.call(ca.querySelectorAll("[" + attr + "]"), function (el) {
      return !isUi(el) && range.intersectsNode(el) && (!needText || hasText(el)) && T.parseSrc(el.getAttribute(attr));
    });
    if (inside.length === 0) {
      const holder = ca.closest("[" + attr + "]");
      return holder && T.parseSrc(holder.getAttribute(attr)) ? { first: holder, last: holder } : null;
    }
    let first = 0;
    while (first + 1 < inside.length && inside[first].contains(inside[first + 1])) { ++first; }
    return { first: inside[first], last: inside[inside.length - 1] };
  }

  // Byte offset in the source of a boundary point inside a data-src element: exact when
  // the element's text is its source bytes (same UTF-8 length), else the element's edge.
  function bytePosition(el, src, container, offset, edge) {
    if (el.contains(container)) {
      const text = T.elementText(el);
      if (T.utf8Length(text) === src.end - src.start) {
        const k = T.offsetInElement(el, container, offset);
        return { byte: src.start + T.utf8Length(text.slice(0, k)), exact: true };
      }
    }
    return { byte: edge === "start" ? src.start : src.end, exact: !el.contains(container) };
  }

  function sourceRangeOf(range) {
    const b = boundaryElements(range, "data-src", false);
    if (!b) { return null; }
    const s = T.parseSrc(b.first.getAttribute("data-src"));
    const e = T.parseSrc(b.last.getAttribute("data-src"));
    if (s.file !== e.file) { return null; }
    const start = bytePosition(b.first, s, range.startContainer, range.startOffset, "start");
    const end = bytePosition(b.last, e, range.endContainer, range.endOffset, "end");
    if (end.byte < start.byte) { return null; }
    return { source_file: s.file, start_byte: start.byte, end_byte: end.byte, exact: start.exact && end.exact };
  }

  function sourceLinesOf(range) {
    const b = boundaryElements(range, "data-src-lines", true);
    if (!b) { return null; }
    const s = T.parseSrc(b.first.getAttribute("data-src-lines"));
    const e = T.parseSrc(b.last.getAttribute("data-src-lines"));
    if (s.file !== e.file) { return { source_file: s.file, first_line: s.start, last_line: s.end }; }
    return { source_file: s.file, first_line: Math.min(s.start, e.start), last_line: Math.max(s.end, e.end) };
  }

  function rectList(range) {
    state.layoutClip = null;
    return G.mergeTextLineRects(rangeFragments(range)).map(function (q) {
      return [round2(q.left), round2(q.top), round2(q.right - q.left), round2(q.bottom - q.top)];
    });
  }
  function round2(v) { return Math.round(v * 100) / 100; }

  function selectionPayload() {
    const sel = root.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) { return null; }
    const range = sel.getRangeAt(0);
    if (isUi(range.commonAncestorContainer)) { return null; }
    const idx = index();
    const span = idx.spanOfRange(range);
    const quote = T.quoteOf(idx.text, span[0], span[1], QUOTE_CONTEXT);
    const rects = rectList(range);
    const b = range.getBoundingClientRect();
    const payload = {
      text: sel.toString(),
      text_quote: quote.exact ? quote : null,
      rects: rects,
      bounds: [round2(b.left), round2(b.top), round2(b.width), round2(b.height)],
      dpr: root.devicePixelRatio || 1,
      scroll: [round2(root.scrollX), round2(root.scrollY)]
    };
    if (attachment()) { payload.page_state = pageState(); }
    const sr = sourceRangeOf(range);
    if (sr) { payload.source_range = sr; }
    const sl = sourceLinesOf(range);
    if (sl) { payload.source_lines = sl; }
    // Inside a revealed answer: the quote for answer feedback (context from the answer).
    const r = state.review;
    if (r && r.revealed) {
      for (const seg of r.ranges) {
        const ss = idx.spanOfRange(seg);
        if (span[0] >= ss[0] && span[1] <= ss[1]) {
          payload.answer = { card_id: r.cardId, text_quote: T.quoteOf(idx.text, span[0], span[1], ANSWER_QUOTE_CONTEXT, ss[0], ss[1]) };
          break;
        }
      }
    }
    return payload;
  }

  function onSelectionChange() {
    clipSelectionToMask();
    root.clearTimeout(state.selectionTimer);
    state.selectionTimer = root.setTimeout(guarded("selection", function () {
      const payload = selectionPayload();
      const json = payload ? JSON.stringify(payload) : "";
      if (json === state.lastSelectionJson) { return; }
      state.lastSelectionJson = json;
      send("selection", payload || { empty: true });
    }), state.options.selectionDelayMs === undefined ? 120 : state.options.selectionDelayMs);
  }

  // ---- position --------------------------------------------------------------------

  function caretNode(x, y) {
    if (doc.caretPositionFromPoint) {
      const p = doc.caretPositionFromPoint(x, y);
      return p ? p.offsetNode : null;
    }
    if (doc.caretRangeFromPoint) {
      const r = doc.caretRangeFromPoint(x, y);
      return r ? r.startContainer : null;
    }
    return null;
  }

  function sourceHolder(node) {
    let el = node && (node.nodeType === 1 ? node : node.parentElement);
    while (el && el !== doc.documentElement) {
      if (el.hasAttribute("data-src") && T.parseSrc(el.getAttribute("data-src"))) { return { el: el, attr: "data-src" }; }
      if (el.hasAttribute("data-src-lines") && T.parseSrc(el.getAttribute("data-src-lines"))) { return { el: el, attr: "data-src-lines" }; }
      el = el.parentElement;
    }
    return null;
  }

  function sourceElementAt(x, y) {
    const els = doc.elementsFromPoint ? doc.elementsFromPoint(x, y) : [doc.elementFromPoint(x, y)];
    for (const el of els) {
      if (!el || isUi(el)) { continue; }
      let found = sourceHolder(el);
      // A paragraph of word spans (Markdown): the word under the point.
      if ((!found || found.attr === "data-src-lines") && el.querySelector && el.querySelector("[data-src]")) {
        const c = caretNode(x, y);
        const inner = c ? sourceHolder(c) : null;
        if (inner && inner.attr === "data-src") {
          const b = inner.el.getBoundingClientRect();
          if (y >= b.top - 2 && y <= b.bottom + 2) { found = inner; }
        }
      }
      if (found) {
        const b = found.el.getBoundingClientRect();
        if (b.height > 0 && y >= b.top - 1 && y <= b.bottom + 1) { return found; }
      }
      break;
    }
    return null;
  }

  function topAnchor() {
    let above = null;
    for (const el of state.anchorList) {
      const b = el.getBoundingClientRect();
      if (b.top >= 0 && b.top < root.innerHeight) { return { key: el.getAttribute("data-key"), visible: true }; }
      if (b.top < 0) { above = el; }
    }
    return above ? { key: above.getAttribute("data-key"), visible: false } : null;
  }

  function getPosition() {
    const pos = {
      scroll_y: round2(root.scrollY),
      scroll_fraction: 0,
      text_size: state.textPercent,
      width: root.innerWidth
    };
    const de = doc.documentElement;
    const range = Math.max(1, de.scrollHeight - root.innerHeight);
    pos.scroll_fraction = Math.min(1, Math.max(0, root.scrollY / range));
    if (attachment()) { pos.page_state = pageState(); }
    const a = topAnchor();
    if (a) { pos.anchor_key = a.key; pos.anchor_visible = a.visible; }
    if (srcList("data-src").length === 0 && srcList("data-src-lines").length === 0) { return pos; }
    const col = doc.body.getBoundingClientRect();
    const xs = [col.left + col.width * 0.5, col.left + col.width * 0.25, col.left + col.width * 0.75, col.left + 8];
    for (let y = 1; y < root.innerHeight; y += 6) {
      for (const x of xs) {
        if (x < 0 || x > root.innerWidth) { continue; }
        const found = sourceElementAt(x, y);
        if (!found) { continue; }
        const src = T.parseSrc(found.el.getAttribute(found.attr));
        const b = found.el.getBoundingClientRect();
        const fraction = b.height > 0 ? Math.min(1, Math.max(0, (0 - b.top) / b.height)) : 0;
        const list = srcList(found.attr);
        let ordinal = -1;
        for (let i = 0; i < list.length; ++i) { if (list[i].el === found.el) { ordinal = i; break; } }
        if (found.attr === "data-src-lines") {
          const count = src.end - src.start + 1;
          pos.source = { source_file: src.file, line: src.start + Math.min(count - 1, Math.floor(fraction * count)),
            first_line: src.start, last_line: src.end };
        } else {
          pos.source = { source_file: src.file, byte: src.start, end_byte: src.end };
        }
        pos.source.fraction = Math.round(fraction * 10000) / 10000;
        // The view's top relative to the element (negative: the element starts lower).
        pos.source.offset_y = round2(0 - b.top);
        pos.source.ordinal = ordinal;
        return pos;
      }
    }
    return pos;
  }

  function sendPosition(force) {
    const p = getPosition();
    const json = JSON.stringify(p);
    if (!force && json === state.lastPositionJson) { return; }
    state.lastPositionJson = json;
    state.lastPositionSent = Date.now();
    send("position", p);
  }

  function onScroll() {
    const wait = state.options.positionThrottleMs === undefined ? 200 : state.options.positionThrottleMs;
    if (state.positionTimer) { return; }
    const elapsed = Date.now() - state.lastPositionSent;
    state.positionTimer = root.setTimeout(guarded("position", function () {
      state.positionTimer = 0;
      sendPosition(false);
    }), Math.max(0, wait - elapsed));
  }

  function fileMatches(a, b) {
    if (!a || !b) { return false; }
    return a === b || a.endsWith("/" + b) || b.endsWith("/" + a);
  }

  function scrollToDocY(y, margin) {
    root.scrollTo(root.scrollX, Math.max(0, y - (margin || 0)));
  }

  function findLineElement(file, line, ordinal) {
    const list = srcList("data-src-lines");
    if (ordinal !== undefined && ordinal >= 0 && list[ordinal] && fileMatches(list[ordinal].file, file) &&
        list[ordinal].start <= line && line <= list[ordinal].end) { return list[ordinal]; }
    let best = null;
    for (const item of list) {
      if (!fileMatches(item.file, file) || item.start > line || item.end < line) { continue; }
      if (item.el.getBoundingClientRect().height <= 0 || !hasText(item.el)) { continue; }
      if (!best || item.end - item.start < best.end - best.start) { best = item; }
    }
    if (best) { return best; }
    // Nearest block after the line, else the last before it.
    let after = null, before = null;
    for (const item of list) {
      if (!fileMatches(item.file, file) || item.el.getBoundingClientRect().height <= 0) { continue; }
      if (item.start > line && (!after || item.start < after.start)) { after = item; }
      if (item.end < line && (!before || item.end >= before.end)) { before = item; }
    }
    return after || before;
  }

  function findByteElement(file, byte, ordinal) {
    const list = srcList("data-src");
    if (ordinal !== undefined && ordinal >= 0 && list[ordinal] && fileMatches(list[ordinal].file, file) &&
        list[ordinal].start <= byte && byte <= list[ordinal].end) { return list[ordinal]; }
    let best = null, after = null;
    for (const item of list) {
      if (!fileMatches(item.file, file)) { continue; }
      if (item.start <= byte && (byte < item.end || (byte === item.end && item.start === item.end))) {
        if (!best || item.end - item.start < best.end - best.start) { best = item; }
      } else if (item.start > byte && (!after || item.start < after.start)) { after = item; }
    }
    return best || after;
  }

  function elementDocTop(el) {
    const b = el.getBoundingClientRect();
    return { top: b.top + root.scrollY, height: b.height };
  }

  function scrollTo(target, opts) {
    opts = opts || {};
    const margin = opts.margin || 0;
    if (typeof target === "string") { target = { anchor_key: target }; }
    if (!target || typeof target !== "object") { throw new Error("scrollTo needs a target"); }
    const src = target.source || target;
    const file = src.source_file || src.sourceFile;
    if (file && (src.line !== undefined || src.byte !== undefined)) {
      const lines = src.line !== undefined;
      const value = Number(lines ? src.line : src.byte);
      const item = lines ? findLineElement(file, value, src.ordinal) : findByteElement(file, value, src.ordinal);
      if (!item) { return { found: false }; }
      const inside = item.start <= value && value <= item.end;
      const t = elementDocTop(item.el);
      let y = t.top;
      if (src.ordinal !== undefined && srcList(lines ? "data-src-lines" : "data-src")[src.ordinal] === item &&
          (typeof src.fraction === "number" || typeof src.offset_y === "number")) {
        // The same element of the same edition (a position from getPosition): back to the same spot.
        y = typeof src.offset_y === "number" && (src.offset_y < 0 || typeof src.fraction !== "number")
          ? t.top + src.offset_y : t.top + Math.max(0, Math.min(1, src.fraction)) * t.height;
      } else if (lines && inside) {
        // A line of a block that spans several lines: proportionally inside the block.
        y = t.top + ((value - item.start) / (item.end - item.start + 1)) * t.height;
      } else if (!lines && inside && value > item.start) {
        const text = T.elementText(item.el);
        if (T.utf8Length(text) === item.end - item.start) {
          // The character at the byte.
          let bytes = 0, k = 0;
          while (k < text.length && bytes < value - item.start) { bytes += T.utf8Length(text[k]); ++k; }
          const r = charRange(item.el, Math.min(k, Math.max(0, text.length - 1)));
          if (r) { y = r.getBoundingClientRect().top + root.scrollY; }
        }
      }
      scrollToDocY(y, margin);
      return { found: true, exact: inside };
    }
    const key = target.anchor_key || target.anchorKey;
    if (key) {
      const entry = state.anchors.get(key);
      const el = entry && (entry.start || entry.point);
      if (!el) { return { found: false }; }
      const r = doc.createRange();
      r.setStartAfter(el);
      const b = r.getBoundingClientRect();
      const top = b.height > 0 || b.top !== 0 ? b.top : el.getBoundingClientRect().top;
      scrollToDocY(top + root.scrollY, margin);
      return { found: true };
    }
    const itemId = target.item_id || target.itemId;
    if (itemId) {
      const p = state.items.get(itemKey(target.kind || "card", itemId)) || state.items.get(itemKey("comment", itemId));
      if (!p) { return { found: false }; }
      const first = p.ranges[0] ? p.ranges[0].getBoundingClientRect() : p.points[0].el.getBoundingClientRect();
      scrollToDocY(first.top + root.scrollY, opts.margin === undefined ? FOCUS_MARGIN * root.innerHeight : margin);
      return { found: true };
    }
    if (typeof target.scroll_y === "number") { root.scrollTo(root.scrollX, target.scroll_y); return { found: true }; }
    if (typeof target.scroll_fraction === "number") {
      const de = doc.documentElement;
      root.scrollTo(root.scrollX, target.scroll_fraction * Math.max(0, de.scrollHeight - root.innerHeight));
      return { found: true };
    }
    throw new Error("unknown scroll target");
  }

  function charRange(el, k) {
    let left = k;
    let result = null;
    T.textNodes(el, function (n) {
      if (result) { return; }
      if (left < n.data.length) {
        result = doc.createRange();
        result.setStart(n, left);
        result.setEnd(n, Math.min(n.data.length, left + 1));
      } else { left -= n.data.length; }
    });
    return result;
  }

  // ---- links, clicks, menus ---------------------------------------------------------

  function parseThoughtsLink(value) {
    const m = /^thoughts-link:([A-Za-z0-9_-]+)(?:\?(.*))?$/.exec(value || "");
    if (!m) { return null; }
    const params = {};
    (m[2] || "").split("&").forEach(function (pair) {
      if (!pair) { return; }
      const i = pair.indexOf("=");
      const k = decodeURIComponent((i < 0 ? pair : pair.slice(0, i)).replace(/\+/g, " "));
      const v = i < 0 ? "" : decodeURIComponent(pair.slice(i + 1).replace(/\+/g, " "));
      params[k] = v;
    });
    return { link_kind: m[1], params: params };
  }

  function linkInfo(a) {
    const href = a.getAttribute("href") || a.getAttributeNS("http://www.w3.org/1999/xlink", "href") || "";
    const data = a.getAttribute("data-thoughts-link");
    const info = { href: href, text: (a.textContent || "").replace(/\s+/g, " ").trim().slice(0, 200) };
    if (data || /^thoughts-link:/i.test(href)) {
      info.kind = "thoughts";
      info.target = data || href;
      const parsed = parseThoughtsLink(info.target) || parseThoughtsLink(href);
      if (parsed) { info.link = parsed; }
    } else if (href.charAt(0) === "#") {
      info.kind = "internal";
      info.target = href;
    } else if (/^https?:/i.test(href)) {
      info.kind = "web";
      info.target = href;
    } else if (/^mailto:/i.test(href)) {
      info.kind = "mailto";
      info.target = href;
    } else {
      info.kind = "other";
      info.target = href;
    }
    return info;
  }

  function internalTarget(hash) {
    let id = hash.slice(1);
    try { id = decodeURIComponent(id); } catch (e) { /* keep raw */ }
    return doc.getElementById(id) || doc.getElementsByName(id)[0] || null;
  }

  function onClick(ev) {
    const t = ev.target;
    const a = t && t.closest ? t.closest("a[href], a[data-thoughts-link], [data-thoughts-link]") : null;
    if (a && !isUi(a) && attachment()) {
      // The page's own links, buttons and #hash routes work as in a browser; the host decides
      // about navigations in its WebView navigation hook. The message is only a hint.
      const info = linkInfo(a);
      info.button = ev.button;
      info.handled = false;
      info.passive = true;
      send("link", info);
      return;
    }
    if (a && !isUi(a)) {
      // Every link goes to the app (WKWebView would hand unknown schemes to the system).
      ev.preventDefault();
      ev.stopPropagation();
      const info = linkInfo(a);
      if (info.kind === "internal") {
        const el = internalTarget(info.href);
        if (el) {
          scrollToDocY(el.getBoundingClientRect().top + root.scrollY, 8);
          info.handled = true;
        }
      }
      info.button = ev.button;
      send("link", info);
      return;
    }
    if (ev.button !== 0) { return; }
    if (t && t.closest && t.closest(".tv-mask-block")) {
      const i = Number(t.closest(".tv-mask-block").getAttribute("data-index"));
      send("maskTapped", { card_id: state.review ? state.review.cardId : null, index: i, point: [ev.clientX, ev.clientY] });
      return;
    }
    const sel = root.getSelection();
    if (sel && !sel.isCollapsed) { return; }
    // A press on one of the page's controls is the page's, not a tap on a highlight.
    if (attachment() && t && t.closest && t.closest(INTERACTIVE)) { return; }
    const hits = itemsAtPoint(ev.clientX, ev.clientY, t);
    if (hits.length) { send("itemTapped", { id: hits[0].id, kind: hits[0].kind, point: [ev.clientX, ev.clientY], items: hits }); }
  }

  function onAuxClick(ev) {
    if (attachment()) { return; }
    const a = ev.target && ev.target.closest ? ev.target.closest("a[href], [data-thoughts-link]") : null;
    if (a && !isUi(a)) { ev.preventDefault(); }
  }

  function onContextMenu(ev) {
    // "app" (default): the app shows its own menu. "native": the platform's selection UI stays
    // (an Android host may need it for long-press selection); the message is sent either way.
    // Attachments: the page's own contextmenu handlers and the platform menu stay, except with
    // selectionMenu "app": on selected text or a feedback highlight outside the page's controls
    // the app shows its menu (app_menu in the message).
    if (state.options.contextMenu !== "native" && !attachment()) { ev.preventDefault(); }
    clipSelectionToMask();
    const t = ev.target;
    const a = t && t.closest ? t.closest("a[href], [data-thoughts-link]") : null;
    const payload = { point: [ev.clientX, ev.clientY], selection: selectionPayload(), items: itemsAtPoint(ev.clientX, ev.clientY, t) };
    if (a && !isUi(a)) { payload.link = linkInfo(a); }
    if (attachment() && state.options.selectionMenu === "app" && !(t && t.closest && t.closest(INTERACTIVE))
        && (payload.selection || payload.items.some(function (item) { return item.kind === "feedback"; }))) {
      ev.preventDefault();
      payload.app_menu = true;
    }
    send("contextmenu", payload);
  }

  // A text field of the page (or the browser's editing host): its keys are never taken.
  function isTextInput(node) {
    const el = node && node.nodeType === 1 ? node : node && node.parentElement;
    if (!el) { return false; }
    if (el.isContentEditable) { return true; }
    const tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") { return true; }
    if (tag !== "INPUT") { return false; }
    const type = (el.getAttribute("type") || "text").toLowerCase();
    return ["button", "checkbox", "radio", "submit", "reset", "image", "range", "color", "file"].indexOf(type) < 0;
  }

  // forwardKeys entry: "[Meta+][Ctrl+][Alt+][Shift+]<key>", <key> being a KeyboardEvent.key
  // ("s", "Escape", "ArrowDown"; letters ignore case) or a KeyboardEvent.code ("KeyS", "Space").
  // Modifiers must match exactly. A letter also matches by its physical key, so Alt+S (macOS: "ß")
  // still is Alt+S.
  function forwardedKey(ev) {
    const list = state.options.forwardKeys;
    if (!Array.isArray(list) || list.length === 0) { return false; }
    const key = String(ev.key || "");
    for (const spec of list) {
      const parts = String(spec).split("+");
      let name = parts.pop();
      if (name === "" && parts.length && parts[parts.length - 1] === "") { parts.pop(); name = "+"; }
      const mods = parts.map(function (p) { return p.toLowerCase(); });
      if (mods.indexOf("meta") >= 0 !== ev.metaKey || mods.indexOf("ctrl") >= 0 !== ev.ctrlKey ||
          mods.indexOf("alt") >= 0 !== ev.altKey || mods.indexOf("shift") >= 0 !== ev.shiftKey) { continue; }
      if (name === key || name.toLowerCase() === key.toLowerCase() || name === ev.code) { return true; }
      if (/^[A-Za-z]$/.test(name) && ev.code === "Key" + name.toUpperCase()) { return true; }
    }
    return false;
  }

  function keyPayload(ev) {
    return { key: ev.key, code: ev.code, meta: ev.metaKey, ctrl: ev.ctrlKey, alt: ev.altKey, shift: ev.shiftKey };
  }

  // Keys with a native action on a focused control (press a button, move a slider, open a list).
  const CONTROL_KEYS = new Set([" ", "Enter", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);

  // A forwardKeys match the app may take: never while composing, in a text field, or once
  // someone has cancelled the key. Attachments also keep a control key on their own focused controls.
  function mayForward(ev) {
    if (ev.isComposing || ev.defaultPrevented || isTextInput(ev.target) || !forwardedKey(ev)) { return false; }
    if (attachment() && CONTROL_KEYS.has(ev.key) && ev.target && ev.target.closest && ev.target.closest(INTERACTIVE)) { return false; }
    return true;
  }

  function sendForwarded(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const payload = keyPayload(ev);
    payload.forwarded = true;
    payload.repeat = !!ev.repeat;
    send("key", payload);
  }

  // Capture phase on the document: before the page's own listeners.
  function onKeyDown(ev) {
    // Editions: the app's own shortcuts (review Skip, Read Aloud, …); the page does nothing with them.
    if (!attachment() && mayForward(ev)) { sendForwarded(ev); return; }
    // Attachments: a forwardKeys match is decided in onKeyDownAfterPage.
    if (attachment() && forwardedKey(ev)) { return; }
    if (!state.options.forwardModifierKeys || !(ev.metaKey || ev.ctrlKey || ev.altKey)) { return; }
    send("key", keyPayload(ev));
  }

  // Bubble phase on the window, attachments only: the page's own listeners have run. A key the
  // page cancelled (preventDefault) or stopped stays the page's; otherwise the app takes it.
  function onKeyDownAfterPage(ev) {
    if (!attachment() || !forwardedKey(ev)) { return; }
    if (mayForward(ev)) { sendForwarded(ev); return; }
    if (state.options.forwardModifierKeys && (ev.metaKey || ev.ctrlKey || ev.altKey)) { send("key", keyPayload(ev)); }
  }

  // ---- layout changes ------------------------------------------------------------

  function relayout() {
    state.rafPending = false;
    // Ranges into DOM the page has replaced are stale: rebuild from the new DOM instead.
    if (state.mutationObserver && pendingMutations()) { flushReindex(); return; }
    state.layoutClip = null;
    if (state.review && state.review.pending) { refreshReview(); } else if (state.review) { layoutReview(false); }
    if (!highlightApi() && state.items.size) { renderHighlights(); }
  }
  function scheduleRelayout() {
    if (state.rafPending) { return; }
    state.rafPending = true;
    root.requestAnimationFrame(guarded("layout", relayout));
  }

  function installListeners() {
    if (state.listeners) { return; }
    state.listeners = true;
    doc.addEventListener("click", guarded("click", onClick), true);
    doc.addEventListener("auxclick", guarded("auxclick", onAuxClick), true);
    doc.addEventListener("contextmenu", guarded("contextmenu", onContextMenu), true);
    doc.addEventListener("dragstart", function (ev) {
      if (attachment()) { return; }
      if (ev.target && ev.target.closest && ev.target.closest("a, img, svg")) { ev.preventDefault(); }
    }, true);
    root.addEventListener("hashchange", guarded("hashchange", function () { if (attachment()) { sendPosition(false); } }));
    doc.addEventListener("selectionchange", guarded("selectionchange", onSelectionChange));
    doc.addEventListener("keydown", guarded("keydown", onKeyDown), true);
    root.addEventListener("keydown", guarded("keydown", onKeyDownAfterPage), false);
    root.addEventListener("scroll", guarded("scroll", onScroll), { passive: true });
    // Scrolling inside a table or a wide formula moves text under fixed overlays.
    doc.addEventListener("scroll", function (ev) { if (ev.target !== doc) { scheduleRelayout(); } }, { capture: true, passive: true });
    root.addEventListener("resize", guarded("resize", function () { relayout(); onScroll(); }));
    root.addEventListener("focus", function () { send("focus", {}); });
    root.addEventListener("blur", function () { send("blur", {}); });
    doc.addEventListener("load", function (ev) { if (ev.target && ev.target.tagName === "IMG") { scheduleRelayout(); } }, true);
    if (doc.fonts && doc.fonts.addEventListener) { doc.fonts.addEventListener("loadingdone", function () { relayout(); }); }
    if (root.ResizeObserver) {
      // Runs after layout and before paint: the mask moves in the same frame as the text.
      state.resizeObserver = new root.ResizeObserver(guarded("resize-observer", function () { relayout(); }));
      state.resizeObserver.observe(doc.body);
      state.observedBody = doc.body;
    }
    if (root.matchMedia) {
      const mq = root.matchMedia("(prefers-color-scheme: dark)");
      const changed = function () { if (state.theme === "system") { rebuildDynamicSheet(); renderHighlights(); } };
      if (mq.addEventListener) { mq.addEventListener("change", changed); }
    }
  }

  // ---- DOM changes (attachment mode) -------------------------------------------------
  // Attachment pages render and re-render their text with their own scripts. A
  // MutationObserver marks the index dirty; ~250 ms after the last change (at most
  // reindexMaxWaitMs after the first) the index is rebuilt, the items placed again and the
  // highlights and mask recomputed. While an answer is hidden this happens at once (before
  // the next paint, so a re-rendered answer never shows). Anything that needs the index
  // (selection, quotes, relayout) flushes pending changes first.

  const TV_CLASS = /(^|\s)tv-\S+/g;
  function relevantMutation(rec) {
    const t = rec.target;
    if (isUi(t)) { return false; }
    const body = doc.body;
    if (!body) { return false; }
    if (rec.type === "attributes") {
      // Layout only (the text index ignores visibility); the viewer's own tv-* classes do not count.
      if (rec.attributeName === "class") {
        const now = t.getAttribute("class") || "";
        return (rec.oldValue || "").replace(TV_CLASS, "").trim() !== now.replace(TV_CLASS, "").trim();
      }
      return true;
    }
    if (t === body || body.contains(t)) {
      if (rec.type !== "childList") { return true; }
      const nodes = Array.prototype.slice.call(rec.addedNodes).concat(Array.prototype.slice.call(rec.removedNodes));
      return nodes.length === 0 || nodes.some(function (n) { return !(n.nodeType === 1 && n.hasAttribute("data-tv-ui")); });
    }
    // A replaced <body>.
    if (rec.type === "childList") {
      const nodes = Array.prototype.slice.call(rec.addedNodes).concat(Array.prototype.slice.call(rec.removedNodes));
      return nodes.some(function (n) { return n.nodeName === "BODY"; });
    }
    return false;
  }

  // → {text: DOM text changed (reindex), layout: only attributes changed (relayout)}.
  function noteMutations(records) {
    let text = false, layout = false;
    for (let i = 0; i < records.length && !text; ++i) {
      if (!relevantMutation(records[i])) { continue; }
      if (records[i].type === "attributes") { layout = true; } else { text = true; }
    }
    if (!text && !layout) { return { text: false, layout: false }; }
    const now = Date.now();
    state.lastMutation = now;
    if (text && !state.dirty) { state.dirty = true; state.dirtySince = now; }
    return { text: text, layout: layout && !text };
  }

  function pendingMutations() {
    if (state.mutationObserver) {
      const changed = noteMutations(state.mutationObserver.takeRecords());
      if (changed.layout) { scheduleRelayout(); }
    }
    return state.dirty;
  }

  function reindexDelay() { return state.options.reindexDelayMs === undefined ? 250 : Number(state.options.reindexDelayMs); }
  function settleTimeout() { return state.options.settleTimeoutMs || 3000; }

  function onMutations(records) {
    const changed = noteMutations(records);
    const r = state.review;
    if (changed.layout) {
      // A tab or panel shown or hidden by a class/style: the mask before the next paint.
      if (r && !r.revealed) { relayout(); } else { scheduleRelayout(); }
      return;
    }
    if (!changed.text) { return; }
    const now = Date.now();
    if (r && !r.pending && !r.revealed && now - state.lastFlush >= 100) { flushReindex(); return; }
    root.clearTimeout(state.reindexTimer);
    const maxWait = state.options.reindexMaxWaitMs === undefined ? 1000 : Number(state.options.reindexMaxWaitMs);
    let wait = Math.min(reindexDelay(), state.dirtySince + maxWait - now);
    if (r && !r.pending && !r.revealed) { wait = Math.min(wait, state.lastFlush + 100 - now); }
    state.reindexTimer = root.setTimeout(guarded("reindex", function () { state.reindexTimer = 0; flushReindex(); }), Math.max(0, wait));
  }

  function observeMutations(on) {
    if (on && !state.mutationObserver && root.MutationObserver) {
      state.mutationObserver = new root.MutationObserver(guarded("mutations", onMutations));
      state.mutationObserver.observe(doc.documentElement, { childList: true, characterData: true, subtree: true,
        attributes: true, attributeOldValue: true, attributeFilter: ["class", "style", "hidden", "open"] });
    } else if (!on && state.mutationObserver) {
      state.mutationObserver.disconnect();
      state.mutationObserver = null;
      state.dirty = false;
      root.clearTimeout(state.reindexTimer);
      state.reindexTimer = 0;
    }
  }

  // Rebuilds the text index, anchors and source lists from the current DOM, places the
  // items again, re-resolves the review, recomputes highlights and mask. A changed
  // placement report is sent as "placement".
  function flushReindex() {
    if (state.flushing) { return null; }
    state.flushing = true;
    try { return rebuildFromDom(); } finally { state.flushing = false; }
  }
  function rebuildFromDom() {
    if (state.mutationObserver) { state.mutationObserver.takeRecords(); }
    root.clearTimeout(state.reindexTimer);
    state.reindexTimer = 0;
    state.dirty = false;
    state.lastFlush = Date.now();
    state.index = null;
    state.srcLists = null;
    state.layoutClip = null;
    if (doc.body && state.resizeObserver && state.observedBody !== doc.body) {
      state.resizeObserver.disconnect();
      state.resizeObserver.observe(doc.body);
      state.observedBody = doc.body;
    }
    scanAnchors();
    let report = null;
    if (state.itemInputs) { report = placeItems(); }
    if (state.review) { refreshReview(); }
    if (attachment()) { rebuildDynamicSheet(); }
    renderHighlights();
    if (report) {
      const json = JSON.stringify(report);
      if (json !== state.lastReportJson) {
        state.lastReportJson = json;
        send("placement", report);
      }
    }
    return report;
  }

  function reindex() {
    const report = flushReindex();
    return { text_length: index().text.length, placement: report };
  }

  // Resolves when fonts and images have loaded and the DOM has not changed for
  // reindexDelayMs (counted from the call), or after maxMs: {quiet}.
  function settle(maxMs) {
    const started = Date.now();
    return whenSettled(maxMs).then(function () {
      return new Promise(function (resolve) {
        const check = function () {
          pendingMutations();
          const now = Date.now();
          const quietSince = Math.max(state.lastMutation, started);
          if (!state.dirty && now - quietSince >= reindexDelay()) { resolve({ quiet: true }); return; }
          if (now - started >= maxMs) { resolve({ quiet: false }); return; }
          root.setTimeout(check, 50);
        };
        check();
      });
    });
  }

  function pageState() { return { hash: root.location.hash || "" }; }

  // Restores a page state recorded with a selection (location.hash): fires the page's
  // hashchange, then "pageState" once the DOM has settled.
  function setPageState(ps) {
    if (typeof ps === "string") { ps = { hash: ps }; }
    ps = ps || {};
    let hash = ps.hash === undefined || ps.hash === null ? "" : String(ps.hash);
    if (hash.length > 512) { throw new Error("page_state.hash is longer than 512 characters"); }
    if (hash && hash.charAt(0) !== "#") { hash = "#" + hash; }
    if (hash === "#") { hash = ""; }
    const before = root.location.hash || "";
    const changed = before !== hash;
    if (changed) { root.location.hash = hash; }
    const gen = ++state.pageStateGen;
    settle(settleTimeout()).then(guarded("page-state", function (res) {
      if (gen !== state.pageStateGen) { return; }
      if (pendingMutations()) { flushReindex(); }
      send("pageState", { page_state: pageState(), requested: { hash: hash }, changed: changed, settled: res.quiet });
    }));
    return { page_state: pageState(), changed: changed };
  }

  // ---- API -------------------------------------------------------------------------

  function whenSettled(timeoutMs) {
    const waits = [];
    if (doc.fonts && doc.fonts.ready) { waits.push(doc.fonts.ready); }
    Array.prototype.forEach.call(doc.images, function (img) {
      if (!img.complete) {
        waits.push(new Promise(function (resolve) {
          img.addEventListener("load", resolve, { once: true });
          img.addEventListener("error", resolve, { once: true });
        }));
      }
    });
    return Promise.race([Promise.all(waits), new Promise(function (resolve) { root.setTimeout(resolve, timeoutMs); })]);
  }

  function sourceMapKind() {
    if (doc.querySelector("[data-src]")) { return "exact"; }
    if (doc.querySelector("[data-src-lines]")) { return "lines"; }
    return "none";
  }

  function load(options) {
    options = options || {};
    if (options.mode !== undefined && options.mode !== "edition" && options.mode !== "attachment") {
      throw new Error("mode must be edition or attachment");
    }
    state.options = options;
    state.mode = options.mode || "edition";
    if (attachment()) {
      // The page keeps its own look: no theme classes, no text size (the app's choice still
      // picks highlight colours when the page has no background of its own).
      doc.documentElement.classList.remove("tv-theme-light", "tv-theme-dark");
      state.textPercent = 100;
      doc.documentElement.setAttribute("data-tv-mode", "attachment");
    } else if (doc.documentElement.getAttribute("data-tv-mode")) {
      doc.documentElement.removeAttribute("data-tv-mode");
    }
    if (options.transport) { state.transport = makeTransport(options.transport); }
    if (typeof options.css === "string" && !state.baseSheet) { state.baseSheet = makeSheet(options.css); }
    if (!state.baseFontPx) { state.baseFontPx = parseFloat(root.getComputedStyle(doc.body).fontSize) || 16; }
    installListeners();
    ensureLayer();
    scanAnchors();
    state.index = null;
    state.srcLists = null;
    if (options.theme) { applyTheme(options.theme); }
    if (typeof options.textSize === "number" && !attachment()) { state.textPercent = clampPercent(options.textSize); }
    rebuildDynamicSheet();
    observeMutations(attachment());
    state.loaded = true;
    const generation = (state.generation = (state.generation || 0) + 1);
    // An attachment renders with its own scripts: ready once its DOM has also been quiet.
    const settled = attachment() ? settle(settleTimeout()) : whenSettled(settleTimeout());
    settled.then(guarded("ready", function () {
      if (generation !== state.generation) { return; }
      if (attachment() && pendingMutations()) { flushReindex(); }
      relayout();
      const anchors = [];
      state.anchors.forEach(function (entry, key) {
        anchors.push({ key: key, kind: entry.point ? "point" : (entry.start && entry.end ? "range" : "unpaired") });
      });
      send("ready", {
        version: VERSION, protocol: PROTOCOL, anchors: anchors, source_map: sourceMapKind(),
        highlight_api: highlightApi(), theme: state.theme, text_size: state.textPercent,
        width: root.innerWidth, height: root.innerHeight, scroll_height: doc.documentElement.scrollHeight,
        warnings: state.warnings.slice(0, 50), css: !!state.baseSheet, mode: state.mode
      });
      sendPosition(true);
    }));
    return { version: VERSION, protocol: PROTOCOL, anchors: state.anchors.size, mode: state.mode };
  }

  function clampPercent(n) {
    n = Number(n);
    if (!isFinite(n)) { throw new Error("text size must be a number (percent)"); }
    return Math.min(400, Math.max(40, n));
  }

  function setTextSize(percent) {
    if (attachment()) {
      // The page's own layout: the text size is not changed.
      clampPercent(percent);
      return { text_size: 100, font_px: parseFloat(root.getComputedStyle(doc.body).fontSize), applied: false };
    }
    const p = getPosition();
    state.textPercent = clampPercent(percent);
    rebuildDynamicSheet();
    if (p.source) { scrollTo(p.source); } else if (p.anchor_key) { scrollTo(p.anchor_key); }
    relayout();
    sendPosition(true);
    return { text_size: state.textPercent, font_px: parseFloat(root.getComputedStyle(doc.body).fontSize) };
  }

  function applyTheme(theme) {
    if (theme !== "light" && theme !== "dark" && theme !== "system") { throw new Error("theme must be light, dark or system"); }
    state.theme = theme;
    if (attachment()) { return; }
    const de = doc.documentElement;
    de.classList.remove("tv-theme-light", "tv-theme-dark");
    if (theme !== "system") { de.classList.add("tv-theme-" + theme); }
  }

  function setTheme(theme) {
    applyTheme(theme);
    rebuildDynamicSheet();
    renderHighlights();
    const result = { theme: theme, background: root.getComputedStyle(doc.body).backgroundColor, color: root.getComputedStyle(doc.body).color };
    if (attachment()) { result.applied = false; }
    return result;
  }

  function copySelection() {
    clipSelectionToMask();
    const sel = root.getSelection();
    return sel ? String(sel) : "";
  }

  function selectQuote(quote, mode) {
    const r = quoteRange(typeof quote === "string" ? { exact: quote } : quote, mode || "best");
    if (!r) { return false; }
    const sel = root.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    return true;
  }

  function info() {
    return {
      version: VERSION, protocol: PROTOCOL, loaded: state.loaded, anchors: state.anchors.size, items: state.items.size,
      review: state.review ? { card_id: state.review.cardId, revealed: state.review.revealed, inverted: state.review.inverted,
        pending: state.review.pending,
        blocks: state.review.blocks.map(function (b) { return [round2(b.left), round2(b.top), round2(b.right - b.left), round2(b.bottom - b.top)]; }) } : null,
      theme: state.theme, text_size: state.textPercent, highlight_api: highlightApi(), queue: state.queue.length, mode: state.mode,
      source_map: sourceMapKind(), text_length: state.index ? state.index.text.length : -1
    };
  }

  const API = {
    load: load,
    setItems: setItems,
    presentReview: presentReview,
    reindex: reindex,
    setPageState: setPageState,
    getPageState: pageState,
    reveal: reveal,
    clearReview: function () { return clearReview(false); },
    setReviewInverted: setReviewInverted,
    reviewMaskBlockCount: function () { return state.review ? state.review.blocks.length : 0; },
    focusReviewMaskBlock: function (i) { return focusBlock(Number(i)); },
    scrollTo: scrollTo,
    setTextSize: setTextSize,
    setTheme: setTheme,
    getPosition: getPosition,
    getSelection: selectionPayload,
    copySelection: copySelection,
    clearSelection: function () { const s = root.getSelection(); if (s) { s.removeAllRanges(); } return true; },
    selectQuote: selectQuote,
    info: info,
    ping: function (x) { return x === undefined ? null : x; }
  };
  const BEFORE_LOAD = new Set(["load", "info", "ping"]);

  function call(name, argsJson) {
    try {
      if (!Object.prototype.hasOwnProperty.call(API, name)) { throw new Error("unknown method " + name); }
      if (!state.loaded && !BEFORE_LOAD.has(name)) { throw new Error("call load first"); }
      let args = [];
      if (argsJson !== undefined && argsJson !== null && argsJson !== "") {
        const parsed = typeof argsJson === "string" ? JSON.parse(argsJson) : argsJson;
        args = Array.isArray(parsed) ? parsed : [parsed];
      }
      // Attachments: calls see the page's current DOM (changes not indexed yet are indexed now).
      if (state.mutationObserver && !BEFORE_LOAD.has(name) && pendingMutations()) { flushReindex(); }
      const result = API[name].apply(null, args);
      return JSON.stringify({ ok: true, result: result === undefined ? null : result });
    } catch (e) {
      return JSON.stringify({ ok: false, error: String(e && e.message || e) });
    }
  }

  root.ThoughtsViewer = {
    version: VERSION,
    protocol: PROTOCOL,
    call: call,
    // Host-provided transport: fn(jsonString). Replaces the one named in load().
    setTransport: function (fn) { state.customTransport = typeof fn === "function" ? fn : null; return true; },
    // Messages not delivered by a transport (queue transport, or a transport error).
    drain: function () { return JSON.stringify(state.queue.splice(0, state.queue.length)); }
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
