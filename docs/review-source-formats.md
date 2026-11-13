<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Review-card source formats

Review cards are tied to their source by marker lines around the answer passage.
The server does not generate cards: clients create them, and the server places, moves and removes their markers.

`review_source_formats.py` owns the adapter contract and suffix registry.
LaTeX uses the existing scanner and `ReviewCardMaskStart`/`ReviewCardMaskEnd` markers.
Markdown uses the existing Pandoc source parser and `thoughts:v1` HTML-comment markers.
The Markdown adapter compares the parsed document structure before and after insertion to reject selections that would alter paragraphs, lists, tables, or code blocks.
Authored source bytes are preserved, with only marker lines and any required final line separator added.

Both adapters return the existing version-2 source-anchor identities, including stable card and segment IDs, source paths, source hashes, content hashes, and source line ranges.
The renderer publishes verified PDF regions for these identities after the anchor build.
Rebuilds relocate the same markers instead of trusting the original PDF rectangles or line numbers.
The common marking, transaction, card persistence, and rebuild workflow does not branch on source format.

## Adding a format

Implement `ReviewSourceAdapter` for selection validation, marker encoding, insertion validation, canonical anchor extraction, and content media type.
Register its supported suffixes only after its renderer can publish verified regions for the same stable card and segment IDs.
Support source-marker removal and passage deletion through `review_source_delete.py`, including overlap protection, before enabling the format for cards.
Test marking, source persistence, rendering, relocation after edits, disappearance of the answer, and deletion together.
Include a mixed-format card and prove that unsupported formats and changed sources fail explicitly.

The current selection interface uses inclusive lines from UTF-8 source snapshots.
Formats with structured or binary sources must define a verified selection projection and a mapping back to their original source before joining this interface; they must not reinterpret model-provided text as original source.
PDF-only fallback for unsupported authored formats is deliberately not automatic.
