// Thoughts HTML viewer: document text, text quotes and source positions.
//
// - TextIndex: the edition's visible text as one normalized string (whitespace runs
//   collapsed to one space, a space between blocks, no text from <svg>/<math>, the
//   viewer's own overlays or marker spans), with a map back to DOM text positions.
//   Quotes (text_quote.exact/prefix/suffix) are cut from this string, so the same
//   normalization is used when a quote is made and when it is found again.
// - Quote matching uses the "skeleton" of Thoughts Desktop's reviewcardfeedback.cpp:
//   NFKD, without spaces, controls, format characters and combining marks (and in a
//   second pass without hyphens); several hits are ranked by how much of prefix and
//   suffix agree, the first hit wins a tie.
// - Source attributes: data-src="<file>:<start_byte>-<end_byte>" (exact, Markdown) and
//   data-src-lines="<file>:<first>-<last>" (lines, LaTeX).
//
// Strict-mode script without import/export (see geometry.js). Defines
// globalThis.ThoughtsViewerText.
(function (root) {
  "use strict";

  const BLOCK_TAGS = new Set(["ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "BODY", "CAPTION", "CENTER", "DD", "DETAILS",
    "DIV", "DL", "DT", "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER",
    "HR", "HTML", "LI", "MAIN", "NAV", "OL", "P", "PRE", "SECTION", "SUMMARY", "TABLE", "TBODY", "TD", "TFOOT", "TH",
    "THEAD", "TR", "UL"]);
  // Subtrees whose text is not document text.
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT", "HEAD", "TITLE", "svg", "SVG", "math", "MATH"]);

  function skipElement(el) {
    if (SKIP_TAGS.has(el.tagName) || SKIP_TAGS.has(el.localName)) { return true; }
    if (el.hasAttribute("data-tv-ui")) { return true; }
    return false;
  }

  function blockOf(node) {
    let el = node.parentElement;
    while (el && !BLOCK_TAGS.has(el.tagName)) { el = el.parentElement; }
    return el;
  }

  const WS = /\s/;

  // Walks text nodes of `rootEl` in document order, skipping non-text subtrees.
  function textNodes(rootEl, visit) {
    const doc = rootEl.ownerDocument || document;
    const walker = doc.createTreeWalker(rootEl, 1 /* SHOW_ELEMENT */ | 4 /* SHOW_TEXT */, {
      acceptNode: function (n) {
        if (n.nodeType === 1) { return skipElement(n) ? 2 /* REJECT */ : 3 /* SKIP */; }
        return 1; /* ACCEPT */
      }
    });
    let n;
    while ((n = walker.nextNode())) { visit(n); }
  }

  function TextIndex(rootEl) {
    this.root = rootEl;
    this.nodes = [];          // Text nodes in document order
    this.nodeFirst = [];      // index of the node's first character in `text` (-1: none)
    this.nodeLast = [];       // index after its last character
    const chars = [];
    const charNode = [];
    const charOffset = [];
    let lastBlock = null;
    let pendingSpace = false;
    const self = this;
    textNodes(rootEl, function (node) {
      const data = node.data;
      const index = self.nodes.length;
      self.nodes.push(node);
      const block = blockOf(node);
      if (block !== lastBlock) {
        if (chars.length > 0) { pendingSpace = true; }
        lastBlock = block;
      }
      let first = -1;
      for (let i = 0; i < data.length; ++i) {
        const c = data[i];
        if (WS.test(c)) {
          if (chars.length > 0) { pendingSpace = true; }
          continue;
        }
        if (pendingSpace) {
          if (chars.length > 0 && chars[chars.length - 1] !== " ") {
            chars.push(" ");
            // A collapsed space maps to the first character after it.
            charNode.push(index);
            charOffset.push(i);
          }
          pendingSpace = false;
        }
        if (first < 0) { first = chars.length; }
        chars.push(c);
        charNode.push(index);
        charOffset.push(i);
      }
      self.nodeFirst.push(first);
      self.nodeLast.push(chars.length);
    });
    this.text = chars.join("");
    this.charNode = charNode;
    this.charOffset = charOffset;
    this.nodeIndex = new Map();
    for (let i = 0; i < this.nodes.length; ++i) { this.nodeIndex.set(this.nodes[i], i); }
    this.skeletons = {};
  }

  // Index in `text` of the DOM point (container, offset): the first indexed character
  // at or after the point (text.length at the end). `asStart`: the point starts a range,
  // so a collapsed space whose whitespace lies entirely before the point is not part of
  // it (a selection starting at a word's first character starts at that character).
  TextIndex.prototype.offsetOf = function (container, offset, asStart) {
    let nodeIdx = container.nodeType === 3 ? this.nodeIndex.get(container) : undefined;
    if (nodeIdx !== undefined) {
      // First character of this node with charOffset >= offset.
      const first = this.firstCharAtOrAfter(nodeIdx, offset);
      // Only collapsed spaces are " " in `text`; one maps to the character after it.
      if (asStart && first >= 0 && this.text[first] === " " && this.charOffset[first] <= offset) { return first + 1; }
      if (first >= 0) { return first; }
      return this.firstCharFromNode(nodeIdx + 1);
    }
    // An element boundary (or an unindexed text node): the first indexed node after it.
    const point = this.root.ownerDocument.createRange();
    try { point.setStart(container, offset); } catch (e) { return this.text.length; }
    let lo = 0, hi = this.nodes.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      // comparePoint < 0: the node starts before the point.
      if (point.comparePoint(this.nodes[mid], 0) < 0) { lo = mid + 1; } else { hi = mid; }
    }
    return this.firstCharFromNode(lo);
  };

  TextIndex.prototype.firstCharAtOrAfter = function (nodeIdx, offset) {
    const first = this.nodeFirst[nodeIdx];
    if (first < 0) { return -1; }
    // Include a collapsed space that maps into this node just before `first`.
    let lo = first > 0 && this.charNode[first - 1] === nodeIdx ? first - 1 : first;
    let hi = this.nodeLast[nodeIdx];
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.charOffset[mid] < offset) { lo = mid + 1; } else { hi = mid; }
    }
    return lo < this.nodeLast[nodeIdx] ? lo : -1;
  };

  TextIndex.prototype.firstCharFromNode = function (nodeIdx) {
    for (let i = nodeIdx; i < this.nodes.length; ++i) {
      // A collapsed space before the node belongs to the gap, not to the node.
      if (this.nodeFirst[i] >= 0) { return this.nodeFirst[i]; }
    }
    return this.text.length;
  };

  // Text index range [start, end) of a DOM Range.
  TextIndex.prototype.spanOfRange = function (range) {
    const start = this.offsetOf(range.startContainer, range.startOffset, true);
    const end = this.offsetOf(range.endContainer, range.endOffset);
    return [Math.min(start, end), Math.max(start, end)];
  };

  // DOM Range for the text index range [start, end).
  TextIndex.prototype.rangeOf = function (start, end) {
    if (start >= end || start < 0 || end > this.text.length) { return null; }
    const doc = this.root.ownerDocument;
    const r = doc.createRange();
    r.setStart(this.nodes[this.charNode[start]], this.charOffset[start]);
    const last = end - 1;
    const lastNode = this.nodes[this.charNode[last]];
    // A surrogate pair's first half: end after both halves.
    const code = this.text.charCodeAt(last);
    const extra = code >= 0xd800 && code <= 0xdbff ? 2 : 1;
    r.setEnd(lastNode, Math.min(lastNode.data.length, this.charOffset[last] + extra));
    return r;
  };

  // ---- quotes ---------------------------------------------------------------

  const DROPPED = /^[\p{Zs}\p{Zl}\p{Zp}\p{Cc}\p{Cf}\p{Mn}]$/u;
  const NONSPACING = /^\p{Mn}$/u;
  const HYPHENS = new Set(["-", String.fromCharCode(0x2010), String.fromCharCode(0x2011)]);

  // skeleton(text): {text, spans}; spans[k] = [start, end) in `text` for the k-th
  // UTF-16 unit of the skeleton.
  function skeleton(text, dropHyphens) {
    const out = [];
    const spans = [];
    let offset = 0;
    for (const ch of text) {
      const w = ch.length;
      if (NONSPACING.test(ch) && spans.length > 0) {
        // A combining mark belongs to the character before it.
        spans[spans.length - 1][1] = offset + w;
        offset += w;
        continue;
      }
      for (const d of ch.normalize("NFKD")) {
        if (DROPPED.test(d) || (dropHyphens && HYPHENS.has(d))) { continue; }
        out.push(d);
        for (let u = 0; u < d.length; ++u) { spans.push([offset, offset + w]); }
      }
      offset += w;
    }
    return { text: out.join(""), spans: spans };
  }

  function commonSuffix(a, b) {
    let n = 0;
    while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) { ++n; }
    return n;
  }
  function commonPrefix(a, b) {
    let n = 0;
    while (n < a.length && n < b.length && a[n] === b[n]) { ++n; }
    return n;
  }

  // Resolves a quote in `haystack`. mode "best" (default): among several hits the one
  // whose context agrees most; "unique": only when the quote occurs exactly once.
  // Returns {start, end, hits} in `haystack` UTF-16 offsets, or null.
  function resolveQuote(haystack, quote, mode, cache) {
    const exact = quote && typeof quote.exact === "string" ? quote.exact : "";
    for (const dropHyphens of [false, true]) {
      const needle = skeleton(exact, dropHyphens).text;
      if (!needle) { return null; }
      const key = dropHyphens ? "h" : "n";
      let hay = cache ? cache[key] : null;
      if (!hay) {
        hay = skeleton(haystack, dropHyphens);
        if (cache) { cache[key] = hay; }
      }
      const hits = [];
      for (let p = hay.text.indexOf(needle); p >= 0; p = hay.text.indexOf(needle, p + 1)) { hits.push(p); }
      if (hits.length === 0) { continue; }
      if (mode === "unique" && hits.length !== 1) { return null; }
      const before = skeleton(quote.prefix || "", dropHyphens).text;
      const after = skeleton(quote.suffix || "", dropHyphens).text;
      let best = -1, bestScore = -1;
      for (const hit of hits) {
        const score = commonSuffix(before, hay.text.slice(0, hit)) + commonPrefix(after, hay.text.slice(hit + needle.length));
        if (score > bestScore) { best = hit; bestScore = score; }
      }
      return { start: hay.spans[best][0], end: hay.spans[best + needle.length - 1][1], hits: hits.length };
    }
    return null;
  }

  // Cuts at most `count` code points from the end (prefix) or start (suffix).
  function lastCodePoints(text, count) {
    const cps = Array.from(text);
    return cps.slice(Math.max(0, cps.length - count)).join("");
  }
  function firstCodePoints(text, count) {
    return Array.from(text).slice(0, count).join("");
  }

  // text_quote for [start, end) of `text`, context limited to [lo, hi).
  function quoteOf(text, start, end, contextLength, lo, hi) {
    lo = lo === undefined ? 0 : lo;
    hi = hi === undefined ? text.length : hi;
    let s = start, e = end;
    while (s < e && text[s] === " ") { ++s; }
    while (e > s && text[e - 1] === " ") { --e; }
    return {
      exact: text.slice(s, e),
      prefix: lastCodePoints(text.slice(lo, s), contextLength).replace(/^ +/, ""),
      suffix: firstCodePoints(text.slice(e, hi), contextLength).replace(/ +$/, "")
    };
  }

  // ---- source attributes -------------------------------------------------------

  const SRC = /^(.+):(\d+)-(\d+)$/;
  function parseSrc(value) {
    const m = value ? SRC.exec(value) : null;
    if (!m) { return null; }
    const a = Number(m[2]), b = Number(m[3]);
    if (!(b >= a)) { return null; }
    return { file: m[1], start: a, end: b };
  }

  function utf8Length(text) {
    let n = 0;
    for (let i = 0; i < text.length; ++i) {
      const c = text.charCodeAt(i);
      if (c < 0x80) { n += 1; }
      else if (c < 0x800) { n += 2; }
      else if (c >= 0xd800 && c <= 0xdbff && i + 1 < text.length) { n += 4; ++i; }
      else { n += 3; }
    }
    return n;
  }

  // Raw text of an element as the index sees it (same skipped subtrees), and the
  // UTF-16 offset of a DOM point inside it.
  function elementText(el) {
    const parts = [];
    textNodes(el, function (n) { parts.push(n.data); });
    return parts.join("");
  }
  function offsetInElement(el, container, offset) {
    const point = el.ownerDocument.createRange();
    point.setStart(container, offset);
    let count = 0, result = -1;
    textNodes(el, function (n) {
      if (result >= 0) { return; }
      if (n === container) { result = count + Math.min(offset, n.data.length); return; }
      if (point.comparePoint(n, 0) > 0) { result = count; return; } // the node starts after the point
      count += n.data.length;
    });
    return result >= 0 ? result : count;
  }

  root.ThoughtsViewerText = {
    TextIndex: TextIndex,
    textNodes: textNodes,
    skeleton: skeleton,
    resolveQuote: resolveQuote,
    quoteOf: quoteOf,
    parseSrc: parseSrc,
    utf8Length: utf8Length,
    elementText: elementText,
    offsetInElement: offsetInElement,
    blockOf: blockOf,
    skipElement: skipElement
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
