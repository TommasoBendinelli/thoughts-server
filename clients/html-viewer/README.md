<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Thoughts HTML viewer

The shared reader for HTML editions (spec: `thoughts-html-view-SPEC.md`, "Shared viewer").
One code base for Thoughts Desktop (Qt WebView: WKWebView / WebView2) and Android (WebView).
Plain JavaScript + CSS, no build step, no runtime dependencies, no network access.

| File | Role |
|---|---|
| `geometry.js` | review mask geometry, a port of the desktop's `part/reviewmaskgeometry.h` + `gui/textlinegeometry.h` (Android's `ReviewMaskGeometry.java` has the same rules) |
| `text.js` | normalized document text, text quotes (the desktop's feedback "skeleton" matching), `data-src` / `data-src-lines` |
| `viewer.js` | the bridge, highlights, review mask, selection, links, position, theme, attachment mode (DOM changes, page state) |
| `viewer.css` | theme overrides, overlay and mask styles; passed to `load()` as text |
| `VERSION` | viewer version (also `VERSION` in `viewer.js`); `PROTOCOL` in `viewer.js` is the bridge version |
| `install-into.py` | vendors the files into an app: `install-into.py <app-asset-dir>` (`--check` for CI, refuses downgrades) and writes `html-viewer.json` (version, protocol, inject order, SHA-256 per file) |

The three scripts are strict-mode scripts **without** `import`/`export`: a host injects them with
`runJavaScript` / `evaluateJavascript`, which works under the edition's CSP `script-src 'none'`
(an ES module would need a `<script type=module>` or `import()`, both blocked). They are still valid
side-effect modules (`import "./geometry.js"`), which is how the Node unit test loads `geometry.js`.

## Host integration

1. Load the edition's `index.html` from the local cache (desktop: loopback server with the strict
   CSP; Android: `WebViewAssetLoader` / `shouldInterceptRequest`). Never the network.
2. After the page has loaded, evaluate `geometry.js`, `text.js`, `viewer.js` in this order
   (a second injection is a no-op).
3. `ThoughtsViewer.call("load", JSON.stringify([options]))`, then wait for the `ready` message.

```js
options = {
  transport: {kind: "http", url: "/<token>/msg"}       // desktop: fetch POST, one message per request
           | {kind: "android", object: "ThoughtsViewerBridge"}  // @JavascriptInterface postMessage(String)
           | {kind: "webkit", handler: "thoughts"} | {kind: "webview2"} | {kind: "queue"},
  css: "<text of viewer.css>",
  theme: "light" | "dark" | "system",
  textSize: 100,                 // percent of the edition's body font size
  maskColor: "#fff",             // optional; default: the page background (blank paper, like the PDF apps)
  forwardModifierKeys: false,    // send "key" for Ctrl/Cmd/Alt keydowns (WebView2 accelerators)
  forwardKeys: [],               // the app's shortcuts, e.g. ["s", "Escape", "Meta+Shift+u"]: sent as
                                 // "key" {forwarded: true} and cancelled in the page, never from a text field
  contextMenu: "app",            // "native": do not cancel the page's contextmenu (Android long press)
  selectionMenu: "native",       // attachment mode, "app": see below
  positionThrottleMs: 200, selectionDelayMs: 120, settleTimeoutMs: 3000,
  mode: "edition",               // "attachment": a page that runs its own scripts (below)
  reindexDelayMs: 250, reindexMaxWaitMs: 1000, reviewTimeoutMs: 3000   // attachment mode
}
```

A host may instead install its own function: `ThoughtsViewer.setTransport(fn)`, `fn(jsonString)`.
Messages that could not be delivered (transport `queue`, or a transport error) wait in
`ThoughtsViewer.drain()` (a JSON array).

### App → viewer

`ThoughtsViewer.call(name, argsJson)`: `argsJson` is a JSON array of positional arguments (a
non-array value is one argument). Returns a JSON string `{"ok":true,"result":…}` or
`{"ok":false,"error":"…"}`. On Android `evaluateJavascript` hands back that string JSON-encoded once more.

| Call | Arguments → result |
|---|---|
| `load(options)` | → `{version, protocol, anchors}`; `ready` follows when fonts and images have settled |
| `setItems(cards, comments, feedback, options?)` | → placement report (below) |
| `presentReview(cardId, {inverted?, scroll?, anchors?, question_anchors?, text_quote?})` | hides the answer → `{blocks}`; error if the card is not shown in HTML. `text_quote`: the answer by quote instead of the card's `setItems` entry. Attachment mode: `{blocks: 0, pending: true}` while the answer is not in the page yet (see below) |
| `reindex()` | rebuilds the text index from the current DOM, places the items again, recomputes highlights and mask → `{text_length, placement}` (placement report or null before `setItems`) |
| `setPageState({hash})` | sets `location.hash` (the page's `hashchange` runs; `""` clears it, a missing `#` is added, ≤ 512 chars) → `{page_state, changed}`; `pageState` follows when the DOM has settled |
| `getPageState()` | → `{hash}` (`location.hash`, `""` when none) |
| `reveal()` | mask → frame around the same blocks → `{blocks}` |
| `setReviewInverted(bool)` | flipped mask (answer visible, everything else hidden) |
| `clearReview()` | removes mask/frame |
| `reviewMaskBlockCount()` | → n (also while revealed, like the desktop) |
| `focusReviewMaskBlock(i)` | scrolls block i to 12 % from the top → bool |
| `scrollTo(target, {margin?})` | `anchorKey` string, `{anchor_key}`, `{source_file|sourceFile, line}`, `{source_file, byte}`, a `getPosition()` result, `{item_id, kind?}`, `{scroll_y}`, `{scroll_fraction}` → `{found, exact?}` |
| `setTextSize(percent)` | keeps the reading position → `{text_size, font_px}` |
| `setTheme("light"|"dark"|"system")` | → `{theme, background, color}` |
| `getPosition()` | → position (below) |
| `getSelection()` | → selection payload (below) or null |
| `copySelection()` | → the selected text (never masked text); the app puts it on the clipboard (⌘C is Qt's) |
| `clearSelection()`, `selectQuote(quote, mode?)`, `info()`, `ping(x)` | helpers |

Items (`setItems`):

```js
cards    = [{id, anchors: [key…], question_anchors?: [key…], text_quote?: {exact, prefix, suffix}, selected?, style?}]
comments = [{id, anchors: [key…], selected_text?, text_quote?, strike?, selected?, style?}]
feedback = [{id, card_id, polarity: "positive"|"negative", text_quote: {exact, prefix, suffix}}]
                                 // attachment mode: without card_id, found in the whole page
options  = {styles?: {name: {background?, color?, underline?, strike?, dark?: {…}}}, highlight_cards?: true,
            show_feedback?: "revealed"|"always"|"never"}
```

Marker keys are opaque: the viewer looks them up in `data-key` of the edition's
`<span class="tm" data-tm="start|end|point">` anchors and never parses them; the app says which
keys belong to which item. A card's answer is the DOM range between `start` and `end` of each of its
segments. Without a placed anchor a card is placed by `text_quote` (a pending "link at next Sync"
card, style `pending`); a comment by `selected_text` when it occurs exactly once (pdf_only), else by
`text_quote`. Feedback is resolved inside its card's answer text. Report:
`{cards: {placed, partial: [{id, missing}], unplaced: [{id, reason}]}, comments: {…}, feedback: {placed, unplaced}}`
(reasons: `no_anchor_in_html`, `quote_not_found`, `selected_text_not_unique_or_missing`,
`card_not_shown`, `orphaned`). Unplaced cards are the "Not shown in HTML view" list.

Styles (defaults, overridable): `card`, `card-selected`, `pending`, `question`, `comment`,
`comment-strike`, `comment-selected`, `feedback-positive`, `feedback-negative`. Highlights use the
CSS Custom Highlight API (no DOM change); without it, overlay boxes. Point anchors get a dot.

### Viewer → app messages

Every message: `{type, v: <protocol>, seq, …}` (`seq` orders HTTP deliveries).

| type | fields |
|---|---|
| `ready` | `version, protocol, mode, anchors: [{key, kind: range|point|unpaired}], source_map: exact|lines|none, highlight_api, theme, text_size, width, height, scroll_height, warnings, css` |
| `selection` | selection payload, or `{empty: true}` when it collapses (debounced) |
| `contextmenu` | `point: [x, y]`, `selection` (payload or null), `items: [{id, kind}]` under the point, `link?`, `app_menu?` (attachment mode with `selectionMenu: "app"`: the page's menu was cancelled, the app shows its own) |
| `itemTapped` | `id, kind` (`card`, `comment`, `feedback`, `question`), `point`, `items` (all hits, most specific first) |
| `maskTapped` | `card_id, index, point` (a tap on a hidden answer block) |
| `link` | `kind: thoughts|web|internal|mailto|other`, `href`, `target`, `link?: {link_kind, params}` (parsed `thoughts-link:<kind>?…`), `text`, `button`, `handled` (internal links are scrolled to by the viewer), `passive: true` in attachment mode (the click was not cancelled) |
| `position` | position, throttled while scrolling, after text size changes and at `ready` |
| `reviewMask` | `card_id, state: hidden|revealed|none|pending|not_found, inverted, blocks` (also when a relayout changes the block count); attachment mode adds `found` (the answer is on screen) |
| `placement` | attachment mode: a placement report (as `setItems` returns) when a DOM change changed it |
| `pageState` | `page_state: {hash}, requested: {hash}, changed, settled` after `setPageState`, once the DOM was quiet for `reindexDelayMs` (`settled: false`: still changing after `settleTimeoutMs`) |
| `focus` / `blur` | the page gained / lost focus (Qt's `focusWidget` goes stale) |
| `key` | `key, code, meta, ctrl, alt, shift` (with `forwardModifierKeys`); `forwarded: true, repeat` for a `forwardKeys` match |
| `error` | `message, where` |

In an edition every link click is cancelled in the page and reported (WKWebView would otherwise hand unknown
schemes to the system); web links too, the app opens them externally. `data-thoughts-link` wins over
`href` when both are present.

Selection payload:

```js
{ text,                                   // what the user sees selected
  text_quote: {exact, prefix, suffix},    // normalized text, prefix/suffix ≤ 64 code points
  source_range?: {source_file, start_byte, end_byte, exact},   // from data-src (Markdown)
  source_lines?: {source_file, first_line, last_line},         // from data-src-lines (LaTeX)
  answer?: {card_id, text_quote},         // inside a revealed answer: context from the answer, ≤ 32
  rects: [[x, y, w, h]…], bounds, dpr, scroll }                 // viewport CSS px, one rect per line
```

Position:

```js
{ anchor_key?, anchor_visible?,          // first marker anchor in view, else the last one above it
  source?: {source_file, line, first_line, last_line, fraction, offset_y, ordinal}   // lines
         | {source_file, byte, end_byte, fraction, offset_y, ordinal},             // exact
  scroll_y, scroll_fraction, text_size, width }
```

`source` describes the element (`data-src` or `data-src-lines`) at the top of the view: `line` is
interpolated inside a block that spans several lines; `fraction`/`offset_y`/`ordinal` let `scrollTo`
return to the same spot in the same edition (also after a text size change). From the PDF view the
app passes only `{source_file, line}`.

## Attachment mode

`load({mode: "attachment", …})` is for HTML attachments: standalone pages that run their own
scripts (tabs, simulators, `#hash` routes), opened inside Thoughts with an opaque-origin sandbox CSP
(`sandbox allow-scripts; default-src 'self' 'unsafe-inline' data: blob:; connect-src 'none'`). The
http transport needs `connect-src`, so hosts use `android`, `webkit` or `webview2`. Cards in
attachments have no markers: they are placed by `text_quote` (style `card`, not `pending`).
Everything else (calls, messages, quotes, mask rules) is the edition's. Differences:

- **The page keeps its behaviour.** Link clicks, `#hash` links, `contextmenu`, `auxclick` and
  `dragstart` are not cancelled (no `stopPropagation`: the page's own listeners run). `link`
  messages still arrive with `passive: true` as a hint; navigation decisions belong to the host's
  navigation hook. `contextmenu` messages are sent as before; with `selectionMenu: "app"` a right click on selected text or a feedback highlight, outside the page's controls, cancels the page's menu and says `app_menu`. A click on the page's controls
  (`a, button, input, select, textarea, label, summary, [role=button|tab|…], [contenteditable]`)
  is never an `itemTapped`.
- **Keys: the page first.** A `forwardKeys` match is decided after the page's own listeners
  (a bubbling `keydown` listener on `window`, instead of the edition's capturing one on the
  document): a key the page cancelled (`preventDefault`) or stopped (`stopPropagation`) stays the
  page's; otherwise it is forwarded and cancelled as in an edition. Text fields keep their keys,
  and `Space`, `Enter`, arrows, `Home`/`End`, `PageUp`/`PageDown` stay with the page's focused
  controls (the selector above). The page's `window` listeners added after the viewer's `load`
  run after the viewer's and cannot keep a forwarded key. A page that uses a forwarded key without
  cancelling it gets it and the app acts too. `forwardModifierKeys` messages are unchanged.
- **No theme or text size on the page.** No `tv-theme-*` class, `theme`/`textSize` load options
  and `setTheme`/`setTextSize` change nothing on the page (`applied: false`; `setTextSize` keeps
  `text_size: 100`); highlight colours use their dark variants when the page's own background is
  dark; the mask is the page's background. Everything the viewer adds stays namespaced
  (`ThoughtsViewer`, `tv-*` classes and highlights, `data-tv-*`; `<html data-tv-mode="attachment">`).
- **The text index follows the DOM.** A `MutationObserver` (childList, characterData, subtree;
  attributes `class`/`style`/`hidden`/`open` for layout only, the viewer's own `tv-*` classes
  ignored) marks the index dirty. `reindexDelayMs` (250) after the last change, at most
  `reindexMaxWaitMs` (1000) after the first, the viewer rebuilds the index and anchors, places the
  `setItems` items again, re-resolves the review and recomputes highlights and mask, and sends
  `placement` when the report changed. While an answer is hidden this happens in the observer
  callback itself (at most every 100 ms), before the next paint, so a re-rendered answer is never
  shown. Every call and every selection, tap or relayout first indexes pending changes.
  `reindex()` does the same on demand. `ready` waits until the DOM has been quiet too.
- **Page state.** `selection` and `position` (and `getSelection`/`getPosition`) carry
  `page_state: {hash: location.hash}`; `position` is also sent on `hashchange`.
  `setPageState({hash})` restores it.
- **Review of a card whose text is not there yet.** `presentReview` returns
  `{blocks: 0, pending: true}` when the answer is not in the page or not on screen (an inactive
  tab), retries after each reindex and relayout, and masks the answer as soon as it appears
  (`reviewMask` `hidden`, `found: true`). Once the page has settled (fonts, images, DOM quiet; at
  most `settleTimeoutMs`) it waits `reviewTimeoutMs` more; then it sends `reviewMask` with
  `state: "not_found", found: false, blocks: 0` and ends the review (the app shows the stored
  answer text). A presented answer that leaves the page later (the user switches tabs) keeps the
  review: `reviewMask` reports `blocks: 0, found: false`, and the mask comes back with the text.
  Typical sequence: `setItems` → `setPageState({hash: card.page_state.hash})` → `presentReview`
  right away (no need to wait for `pageState`).
- **Limitations.** Text inside `<svg>`, `<canvas>`, `<math>`, form controls (`input` values,
  `textarea`) and iframes is not in the index and cannot be quoted or masked by quote; text the
  page draws on a canvas is invisible to the viewer. Hidden text (`display: none` tabs) is in the
  index but has no boxes, so it counts as "not on screen". Page state is `location.hash` only: a
  page that keeps its tab in a JavaScript variable (and does not listen to `hashchange`) cannot be
  restored, and its cards are found only on the tab that is shown when the review starts. Pages run
  in the same JavaScript world as the viewer: page scripts can call `ThoughtsViewer` and the bridge,
  so the app treats every message as a UI hint (creating or changing a card needs a native user
  action).

## How things work

- **Text quotes.** The viewer's text is the edition's text nodes in document order (not `<svg>`,
  `<math>`, `<script>`/`<style>` or the viewer's overlays), whitespace runs collapsed to one space
  and a space between blocks. `exact` is the trimmed selection in that text; `prefix`/`suffix` are
  up to 64 code points around it. Quotes are found again with the desktop's rule
  (`part/reviewcardfeedback.cpp`): compare NFKD skeletons without spaces, controls, format
  characters and combining marks (second pass also without hyphens); among several hits the one
  whose prefix/suffix agree most, the first on a tie (`unique` mode: exactly one hit or nothing).
- **`source_range` (exact byte offsets).** The first and last innermost `data-src` elements the
  selection touches give file and bytes. Inside such an element the boundary's byte is
  `start + utf8Length(elementText[0:k])` (k = UTF-16 offset of the boundary in the element's text)
  **when the element's text is its source** (its UTF-8 length equals `end - start`, true for plain
  Markdown words); otherwise (smart quotes, entities, escapes, math) the element's start (for the
  selection start) or end (for the selection end) is used and `exact` is false. A boundary in the
  whitespace between word spans takes the next word's start / the previous word's end. Different
  files at the two ends: no `source_range`.
- **`source_lines`.** First and last `data-src-lines` element with text that the selection touches.
- **Review mask.** Answer segments → text line boxes per text node part (`Range.getClientRects`) and
  the boxes of formulas/images (clipped by scrolling ancestors such as wide tables) → the PDF apps'
  rules in `geometry.js`, with coordinates divided by the text column width so the page-width
  constants keep their meaning. The "page words" the rules check (join across ≤ 3 blank lines only
  when the joined rectangle hides no other text; table padding up to nearby text) are the words and
  objects within four lines above and below the answer. Blocks are sorted top-to-bottom. Overlay
  divs sit in a layer at the document origin (scrolling needs no recompute); relayout on resize
  (`ResizeObserver`, before paint), text size, theme, font loading, image loading and scrolling
  inside nested containers. While hidden, elements entirely inside the answer get `tv-masked`
  (`user-select: none`, `visibility: hidden`) and selections are clipped at the answer's edge, so
  masked text cannot be selected or copied. The question segment (`question_anchors`) is never
  masked; during the review it is highlighted with the `question` style (the PDF apps show the
  question as card text).
- **Theme.** `html.tv-theme-dark|light` overrides the edition's `prefers-color-scheme` rules
  (background, text, links, table rules; figures keep a light card). The mask colour follows.

## Tests

```
node clients/html-viewer/tests/geometry.test.mjs           # geometry, Android's ReviewMaskGeometryTest
PLAYWRIGHT_CORE=…/node_modules/playwright-core \
  nice node clients/html-viewer/tests/run.mjs --browser chromium   # or --browser webkit
```

`run.mjs` serves the edition from a fake origin with the desktop's CSP (`script-src 'none'`, an
inline script is checked to be blocked), injects the scripts like a host, uses the HTTP transport,
and fails on any request outside the edition. It covers a synthetic Markdown-style edition (exact
source map, tables, split cards, links, comments, quotes) and the server-built golden editions in
`tests/golden/` (`synthetic-latex`, `synthetic-markdown`, `thoughts_manual`: copies of the server's
`tests/fixtures/html-editions`; `--goldens DIR` / `THOUGHTS_HTML_GOLDENS` for others). Cards and comments
come from each `manifest.json` `items`; every card must be fully masked at 900 px, 412 px and in dark
mode, a selection in each answer gives `text_quote` and `source_lines` (LaTeX) or an exact
`source_range` (Markdown), and `data-thoughts-link` links reach the app. Plus every `sample-editions/*/tex4ht` of the LaTeX spike when
`--samples DIR` / `THOUGHTS_HTML_SAMPLES` (default `~/repos/thoughts-html-spike/latex/sample-editions`) exists.

The attachment tests load `tests/fixtures/attachment/index.html` (its script renders the text
400 ms after load, routes three tabs by `location.hash`, re-renders the simulator on slider input)
with the attachment sandbox CSP and an Android-style bridge object: placement after render and
re-render, highlights on replaced nodes (Highlight API and overlay fallback), `setPageState` +
`presentReview` masking a hash-routed tab also across a re-render, `not_found`, and the page's own
link/hash/contextmenu/dragstart handling, theme and text size left alone, `forwardKeys` after the
page's listeners, and a selection from an answer's first character (or the gap before it) on a
re-rendered tab.

On a Linux host without root (e.g. the builder), WebKit needs `libgstreamer-plugins-bad1.0-0`,
`libavif16` and their libraries (`apt-get download` + `dpkg-deb -x`, copied into the Playwright
WebKit bundle's `minibrowser-wpe/sys/lib`, plus `LD_LIBRARY_PATH` for Playwright's dependency check).
