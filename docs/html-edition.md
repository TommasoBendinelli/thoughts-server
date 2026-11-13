<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# HTML editions

Every PDF built from a LaTeX or Markdown source gets an **HTML edition**: a reflowing,
script-free reading copy that the apps show beside the PDF (spec:
`thoughts-html-view-SPEC.md`). Imported PDFs have none. Code: `src/thoughts_server/html_edition/`.

## When it is built

1. A build publishes PDFs as before. For every published source document (also an
   unchanged one, and a targeted no-op build) the publication transaction queues one
   `html_editions` row, keyed by the PDF's SHA-256, with `status = pending`
   (`jobs.queue_edition`). An edition that is pending, running or ready for the same PDF
   bytes is kept; a failed one is retried from the new build. A ready edition records the
   SHA-256 of the source files it was converted from (`result_json.inputs`: the document's
   source and every file its source positions name); when one of them differs in the new
   build it is converted again, because markers placed for a card or comment (comments in a
   Markdown source) change the anchors without changing the PDF bytes. While it converts,
   the Library lists the PDF's `html_status` as `pending`.
2. The native build's private workspace is the exact source snapshot the PDF was rendered
   from (sources, build inputs and the PDF build's `.bbl`). Instead of deleting it, the build
   worker moves it to `state/html-edition-inputs/<build id>` (`jobs.keep_inputs`, one
   rename). A build that ran in the shared worktree copies the published projects instead.
3. A daemon thread of the same worker process (`jobs.HtmlEditionWorker`, started by
   `thoughts-worker`) converts the queued editions one at a time under `nice -n 19`, stores
   every file and the manifest as immutable objects and records `ready` or `failed` with the
   error. It removes a kept workspace once no queued edition needs it.

Nothing in this path can fail, delay or roll back a build, a publication or a Sync: queueing
catches its own errors, the conversion runs after the build job has finished, and an HTML
failure only sets `html_status: failed`. An edition whose PDF was superseded before it was
built is skipped ("superseded by a newer PDF"). A conversion is limited to
`THOUGHTS_HTML_EDITION_TIMEOUT_SECONDS` (default 120 s); `THOUGHTS_HTML_EDITIONS=false`
turns the whole feature off.

Multi-library: every Library's worker child has its own queue, objects and state. For a
sandboxed Library (`THOUGHTS_SANDBOX_BUILDS`, on by default on Linux) make4ht and the
PDF-figure conversion run in the same bubblewrap sandbox as its PDF compiles
(`build_sandbox.wrap`): no network, only its own snapshot writable. make4ht always runs in
its own build directory with a private HOME, so no Lua configuration (`.make4ht`, `*.mk4`)
can come from a document folder; the directory is made anew for every attempt, so a TeX
run cannot leave one for the next attempt either.

Existing PDFs get an edition at their next build (also an unchanged rebuild).

## Converters

- **LaTeX** (`latex.py`): make4ht/tex4ht in DVI mode with MathML output, using the PDF
  build's wrapper and search paths, but with `tex/thoughts-html-layer.tex` instead of the
  review instrumentation and `tex/thoughts-html.cfg`. The layer turns markers into anchors,
  `\SourceLabel` into label spans and paragraph starts/ends into source-line spans. Fixes
  carried in the configuration: tex4ht's `\PauseMathClass` bug (TL2026), `\includegraphics`
  and `\pdfximage` as plain `<img>`, acmart's refusal of tex4ht's `\section` (its end-of-document
  check is told tex4ht's definitions are the originals), natbib labels with `\mbox` (ACM),
  and fonts tex4ht has no `.htf` for (Carlito, Inconsolata): tex4ht aborts on them, so the
  converter aliases each missing font to a base font of the same encoding and runs again
  (`state/html-edition-fonts` remembers the aliases).
- **Markdown** (`markdown.py`): the PDF build's Pandoc `commonmark_x+sourcepos` parse
  (`markdown_source.parse_markdown(..., marker_anchors=True)`), its link mapping and the
  project stylesheet; no WeasyPrint.
- **Bundle step** (`bundle.py`, shared): math (MathML from tex4ht, TeX from Pandoc) becomes
  inline MathJax 4.1.3 SVG (`markdown_math/render-edition.mjs`, glyphs in `currentColor`);
  PDF figures become SVG (`pdftocairo`), or a 200 dpi PNG when the SVG would exceed 1.5 MB
  (plots with very many points); images get `assets/<sha256 prefix>.<ext>` and their
  intrinsic `width`/`height`; links are rewritten (below); scripts, frames, forms, event
  handlers, `<link>`/`<meta>` and external references are removed; one `style.css` (converter
  CSS without imports or external `url()`, then a reading stylesheet with light/dark mode).
- **Validator** (`bundle.validate`): an edition is refused (`failed`) if any file has a
  script, an event handler, a frame/object/form, `javascript:`, a stylesheet import, a
  reference to anything outside the edition (except `http(s):`/`mailto:` navigation links),
  an SVG entity or `foreignObject`, or a file name outside the bundle.

## Manifest and objects

The manifest object (`application/vnd.thoughts.html-edition+json`):

```json
{
  "kind": "thoughts_html_edition", "schema": 1,
  "document_id": "…", "source_format": "latex|markdown",
  "source_snapshot_sha256": "…", "pdf_sha256": "…",
  "converter": {"name": "make4ht", "version": "v0.4e", "math": "mathml+mathjax-4.1.3", "edition": "1"},
  "entry": "index.html",
  "files": {"index.html": "<sha256>", "style.css": "<sha256>", "assets/3f2a….svg": "<sha256>"},
  "anchors": ["card:4150ddf3-…:0", "comment:mjpdf-aee1…"],
  "items": {"cards": {"4150ddf3-…": {"anchors": ["card:4150ddf3-…:0"], "question_anchors": []}},
            "comments": {"mjpdf-aee1…": ["comment:mjpdf-aee1…"]}},
  "source_map": "exact|lines",
  "source_files": {"docs-notes/notes.md": "<sha256>"},
  "warnings": ["…"]
}
```

`items` says which marker keys belong to which card or comment, so a viewer never parses keys.
`source_files` (Markdown) gives the SHA-256 of each file that `data-src` positions refer to;
an app copies it into `source_range.source_sha256`.

Files are fetched through `GET /v1/objects/{sha256}` like PDFs (media types `text/html`,
`text/css`, `image/svg+xml`, `image/png`, …). HTML and SVG objects are served with
`Content-Security-Policy: default-src 'none'; …; sandbox` and `X-Content-Type-Options: nosniff`
so they never run as pages of the API origin; the apps load them from their own cache.

## HTML conventions

- Marker anchors, at the exact text position, in both formats:
  `<span class="tm" data-tm="start|end|point" data-key="KEY"></span>`.
  One key spelling for both formats, the keys the build's staging scanner and the source
  publisher use: `card:<card id>:<segment id>` for review-card answer segments
  (`\ReviewCardMaskStart{id}{seg}`, Markdown `review:<id>:answer:<seg>`) and
  `comment:<comment id>` for comments (`\CommentHighlight`/`\CommentAnchor`, Markdown
  `comment:<id>:<seg>`; a comment's segments are its start/end pairs in document order).
  Markdown question segments (`review:<id>:question:<seg>`) become `card-question:<id>:<seg>`.
  A marker inside a formula is moved just before the formula (a warning says so).
- Source positions: Markdown fragments keep their ids and carry
  `data-src="<docs-root-relative file>:<start byte>-<end byte>"` (`source_map: "exact"`);
  LaTeX blocks (paragraphs, headings, list items, table cells, captions) carry
  `data-src-lines="<docs-root-relative file>:<first>-<last>"` (`source_map: "lines"`).
- `\SourceLabel{l}` and Markdown `{#source-label:l}` headings: `id="source-label:l"`.
- Document links: `<a href="thoughts-link:KIND?k=v&…" data-thoughts-link="(same)">`, values
  percent-encoded (`/` kept), parameters as below. Web links stay `https:`/`mailto:` links
  (the viewer reports every click to the app); internal references stay `#id`. A link the
  PDF also leaves inactive, or a relative file link with no place in the edition, loses its
  `href` and gets `class="thoughts-link-inactive"` (and a `title` with the reason).

| kind | from | parameters |
|---|---|---|
| `pdf` | `\PdfLink{key}{anchor}`, Markdown `paper.pdf#anchor` | `folder` (the linking folder), `key`, `anchor` (`document-start` or a `doc-anchor:` name), `filename` (`<key>.pdf`) |
| `cross-pdf` | `\CrossPdfLink{docs-x/key}{anchor}` | `folder` (`docs-x`), `key`, `anchor`, `filename` |
| `source` | `\SourceLink{path}{label}`, Markdown `other.md#source-label:l` | `path` (docs-root-relative target source), `label` (`document-start`, `source-label:<l>` or a TeX label), `destination` (the PDF named destination, when known), `filename` (the target's PDF, when the build knew it) |
| `cross-source` | `\CrossSourceLink`, Markdown `../../docs-x/…` | as `source` |
| `html` | `\href{thoughts-html:…}`, Markdown `x.html` | `path` (docs-root-relative HTML attachment) |

In the HTML view a `source`/`cross-source` link opens the target's edition at
`#<label>` (`source-label:…`) or its start; `destination`/`filename` open the PDF instead.

## State for clients

Capability `html_edition_v1`.

- Library entries (`/v1/library`, `/v2/library`, `/v1/document-sync`) and the render-status
  publication identity (`/v1/render-status`, `/v1/render-state` documents) carry
  `html_status` (`pending|ready|failed`, or `null` before one is queued),
  `html_edition_sha256` (when ready) and `html_error` (when failed). These are hints: they
  are left out of `library_revision`, the sync-state revision and the Library and
  document-sync ETags, because an edition finishes (or is queued again by a rebuild) in the
  background without a new PDF, and clients must not install a new Library generation for
  it. A listing stored by a client (or answered `304` for its ETag) can therefore be stale.
  The render-state cache and the notification socket (`html_editions_revision`) do change
  with an edition's state. A client asks `GET /v1/html-editions/{pdf_sha256}` for a version
  it has stored as `pending` (with a bounded backoff); `ready` with a known edition and
  `failed` are final for that PDF version.
- `GET /v1/html-editions/{pdf_sha256}`: the edition of any PDF version (current or older):
  `{pdf_sha256, filename, document_id, source_format, html_status, html_edition_sha256,
  html_error, manifest}` (`manifest` when ready); 404 when that PDF has none.

## Cards, comments and feedback made in the HTML view

A card's `primary` or a comment's `origin` made from an HTML selection carries:

```json
{"created_in": "html", "html_edition_sha256": "…",
 "text_quote": {"exact": "…", "prefix": "…", "suffix": "…"},
 "source_range": {"source_file": "docs-x/notes.md", "source_sha256": "…", "start_byte": 0, "end_byte": 0,
                  "exact": true}}
```

`source_range` only for Markdown (`source_map: "exact"`). A card also needs the edition's
`pdf_stem` and `pdf_sha256` (the PDF built from the same snapshot), has no `page`, `rect` or
`regions`, and is saved with `source_link.state = "pending_server_link"` (a page-less card that
was not made in the HTML view is never guessed into place: it becomes `needs_attention`); a comment's `origin` keeps `collection`,
`document_id`, `pdf_sha256` and may omit `page_index`.

The server places the markers (cards: `review_source_link._prepare_html_link`, comments:
the anchor worker with `html_edition.comment_markers`):

1. `source_range`, while its file still has `source_sha256` (unless `exact` is false).
2. Otherwise the quote in the document's current sources (`review_source_link.quote_passages`):
   letters and digits only, so whitespace and markup do not matter; up to 80 source letters
   between words are allowed (a formula is an image in the HTML but letters in the source;
   citations and numbers are spelled differently); a repeated quote is decided by its
   prefix/suffix.
3. Otherwise the existing answer-text fallback (longer quotes).

TeX passages are marked inline when they are plain words that occur once, else as whole
lines (cards) or a point marker before the passage (comments); Markdown passages as whole
lines (cards) or inline/block comment markers. A selection that cannot be placed uniquely
makes the card `needs_attention` (`review_card_place_again`) or the comment's scope
`needs_attention` (`html_selection_unresolved`); nothing is saved PDF-only. The next build
gives the card PDF regions through the usual evidence path.

Answer feedback (`review_card_feedback`) made in the HTML view has `view: "html"` and
`html_edition_sha256`; `anchor.quote` is its anchor and `anchor.pdf` is optional (required,
with at least one region, for `view: "pdf"`, the default). Both fields are fixed by the
first write.

## Retention and version pinning

Edition objects are retained like PDFs (`pdf_retention.retire_superseded_pdfs`): when a
superseded PDF is retired, its edition's manifest and files go with it, except files that
another kept edition also uses. A record that names an edition — `html_edition_sha256` of a
feedback record, `origin.html_edition_sha256` of a comment, or `review_version.html_edition_sha256`
of any record — keeps that edition and the PDF it was built beside.

## Operations

- Requires make4ht/tex4ht, `dvisvgm`, `pdftocairo` (TeX Live scheme-full and poppler-utils on
  Fedora), Node.js with `markdown_math`'s locked runtime (`npm ci`), and the
  `markdown-renderer` extra (Pandoc 3.9) for Markdown.
- Schema migration `0024_html_editions` (run `alembic upgrade head`, also for every
  registry Library).
- Real-Library pass rate (2026-10-26, Fedora, TL2026): 127/127 documents (119 LaTeX, 8 Markdown)
  converted, 220/220 marker anchors exact, 0/2558 formulas failed, median 3 s per LaTeX document
  (measured on the maintainer's Library).
