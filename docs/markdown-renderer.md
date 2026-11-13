<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Markdown source documents

`thoughts-render-markdown` builds Markdown directly through Pandoc HTML and
WeasyPrint, without a LaTeX intermediate or compiler.
Markdown projects can now use the native project build, source artifacts,
PDF-to-source navigation, raw source editing, and source Sync.
The standalone command remains useful for isolated renderer checks.

## Installation and local use

Install the `markdown-renderer` extra in the server environment and WeasyPrint's
native libraries (including Pango).
The source-position and layout adapters require Pandoc 3.9 and WeasyPrint 70.0;
the extra pins the Python distributions that provide these versions.
Upgrading either requires revalidation, not bypassing the version check.

For equations, install the separate locked MathJax 4.1.3 runtime:

```sh
pip install -e '.[dev,markdown-renderer]'
npm ci --prefix src/thoughts_server/markdown_math --ignore-scripts
thoughts-render-markdown /project/markdown/source/example.md \
  --project-root /project \
  --stylesheet /project/markdown/support/document.css \
  --asset /project/markdown/assets/figure.png \
  --output /tmp/example-markdown-build
```

On macOS, the runtime may need `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` to
locate the already installed native libraries.
The output must be a new directory.
Every referenced asset, CSS import, or explicit font file must be supplied with
`--asset`; resource URLs resolve relative to the source or importing stylesheet.
Ordinary web hyperlinks remain clickable, but rendering never downloads their
targets or reads files outside the supplied snapshot.
Missing assets or failed CSS imports fail the build instead of silently omitting
content.

## Source markers

The current profile is `thoughts-commonmark-v1`, using Pandoc's
`commonmark_x+sourcepos+tex_math_dollars` reader.
Each range uses a logical item identity and a segment identity:

```markdown
Before <!-- thoughts:v1:start comment:c123:s1 -->selected **words**<!-- thoughts:v1:end comment:c123:s1 --> after.

<!-- thoughts:v1:point comment:c456:s1 -->

A point before this paragraph adds no layout element.
```

Block markers must occupy their own lines, with blank lines around the enclosed
content.
Ranges may cross or nest; each segment must have one start/end pair.
Repeated table headers produce several placements for the same source fragment.
`review:r123:question:s1` and `review:r123:answer:s1` are also recognized identities;
this does not yet implement review generation or masking.
Markers inside literal code are ordinary code.
Malformed, duplicate, empty-range, unsupported-version, and proposal markers
fail explicitly.
Optional comment text is stored in a separate metadata marker:

```markdown
<!-- thoughts:v1:meta {"key":"comment:c123:s1","contents":"Review this passage.","revision":0} -->
```

Its key must identify an existing source marker.
Escape HTML comment terminators in JSON strings (for example, use `\u003e`
for `>`).
Moving a marked passage preserves its logical comment identity.

## Build evidence

The result contains `document.pdf`, `manifest.json`, and content-addressed `blobs/`.
The manifest has its own discriminator, `markdown_renderer_evidence`, schema 1.
It is **not** a `ReviewCorrespondence` publication proof and explicitly reports
`publication_proof: false`.

The manifest records:

- Original UTF-8 byte ranges, including original CRLF bytes, and hashes for every
  source fragment and the full source.
- Normalized top-left rectangles with zero-based page indexes and separate
  painted-fragment indexes for line continuations and repeated headers.
- Point affinity to the beginning or end of a verified visible source fragment.
- Original source, used assets, styles, actual font bytes, generated equation
  SVGs, renderer code, package/runtime versions, and their hashes.
- An input identity covering these dependencies, the final PDF hash, and a build
  identity binding both together.

The dependency record captures what was used for this build; replaying captured
system fonts on a different machine is not yet implemented.
The API accepts captured byte strings so caller-owned files cannot change between
layout and verification.
The command reads explicit inputs first, renders and verifies, then exposes one
complete output directory.
The standalone command does not publish or synchronize its output.
The project adapter described below packages that evidence for Thoughts.

## Placement validation

Temporary source spans are removed **before** shaping.
Character ownership is kept beside the DOM, with metadata attached only to
existing elements.
This avoids the kerning and punctuation shifts caused by wrapping each token in
an additional HTML span.
The versioned adapter consumes every rendered text/replaced leaf box, including
multiple continuations of one token on the same page.
It matches the shaped text to the ownership sequence and then to actual PDF
glyphs, exporting the glyph bounds for each source fragment.

Each build also renders without metadata and checks unchanged page dimensions,
pagination, text, drawing/image geometry, and native hyperlink regions.
Each source token must have complete painted coverage; a repeated occurrence must
contain the whole token again.
An unverified placement prevents a successful build.

Inline/display equations use MathJax SVG and verify complete PDF drawing objects.
Equation symbols are not ordinary selectable PDF text and do not have individual
source mappings.
Raster images require a corresponding PDF image object.
Raw HTML, transformed/clipped/translucent/RTL layouts, missing glyphs, empty
point-only documents, and a shaped glyph crossing distinct source fragments are
currently rejected.
General SVG assets and arbitrary rendering profiles are not claimed as supported.

## Validation and remaining integration

```sh
pytest tests/test_markdown_renderer.py
pytest tests/test_native_renderer.py tests/test_review_correspondence.py \
  tests/test_source_offsets.py tests/test_agent_proposals.py \
  tests/test_proposal_creation_metadata.py
```

Markdown tests require the optional renderer dependencies; equation tests also
require the locked Node runtime.
The tests cover UTF-8/tabs/CRLF, code literals, crossing comments, long code across
lines/pages, repeated headers, moved points, links, SVG equations, dependency
capture, version guards, and failed builds without output.

The next milestone is a discriminated Markdown publication proof and source
bundle contract accepted by the server, Desktop, and Android before enabling
Markdown discovery.
Reverse selection/insertion, comment metadata reconciliation, proposal branches,
partial application/undo, review masking, offline edits, and Sync still require
their format adapters and isolated end-to-end verification.


## Project build and Sync

An explicit `docs-X/thoughts-project.json` selects the documents in reading order:

```json
{
  "schema_version": 1,
  "documents": [
    {
      "format": "markdown",
      "source": "markdown/source/main.md",
      "title": "My document",
      "stylesheet": "markdown/support/document.css"
    }
  ]
}
```

`title` and `stylesheet` are optional.
The project publishes as `X.pdf` through the existing Library enrollment and
source publisher.
An optional `id` preserves the document identity when its source path moves;
it defaults to `docs-X/markdown/source/main.md`.
An optional `output` names the component PDF.
Explicit manifests can mix Markdown and existing LaTeX documents.
Projects without this manifest retain legacy TeX discovery.
Markdown-only projects require qpdf and the Markdown runtime, but no LaTeX tools.

Authored Markdown, CSS, and the project manifest enter the normal source snapshot.
Assets under `markdown/assets` enter the reproducible build-input snapshot.
The `source-closure-v2` policy verifies consumed source and asset hashes;
legacy TeX projects retain `tex-closure-v1`.
The aggregate PDF must preserve the component's page dimensions, glyphs,
drawings, and image positions before publication succeeds.

Markdown segments use `source_format: markdown`, a
`thoughts-markdown-source-map-v1` page index, and a hash-addressed renderer
evidence object.
They contain no fabricated SyncTeX file.
Published comment placements use resolver 4 and bind the source bytes, marker
identity, painted fragment, component PDF, aggregate PDF, and source bundle.
Clients advertise `markdown_source_v1`; older clients cannot activate these
artifacts through the source Sync plan.
The Android and desktop adapters open the source using the verified page index.
Editing source uses the existing local save and explicit Sync machinery.
The previous verified PDF/source artifact remains active until rebuilding
succeeds, including for hidden metadata edits.

This step supports raw Markdown editing and placement of existing Markdown
comment markers.
Creating those markers from new PDF annotations, Markdown proposal accept/reject,
and review-card generation/masking are not implemented by this adapter.
Do not route Markdown through the TeX marker or proposal mutators.
Server and client deployment are separate from building these changes.
