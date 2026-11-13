<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Document feedback

While reading any Library PDF, the user selects a passage and marks it **liked** (positive) or
**disliked** (negative), optionally with a comment. It is the review card feedback of
`review-card-feedback.md` without a card: the record belongs to the document and to the exact
PDF snapshot that was displayed, not to a review card. It is kept for good, next to review
cards, as a second kind of study data: what the user liked and disliked in each version of a
document.

Status: server `document_feedback_v1` (branch feature/document-feedback). Android implements
§5–§6. Desktop: not yet.

Everything that is not stated here is as in `review-card-feedback.md`. The section numbers
"RCF §n" refer to that file.

## 1. Record

- `entity_type`: `"document_feedback"`
- `collection`: `"document-feedback"`
- `entity_id`: a random canonical (lowercase) UUIDv4 created by the client, equal to
  `payload.feedback_id`
- Delivered by `/v1/changes` (including tombstones), `/v1/projections` (non-deleted only) and
  the `/v1/document-sync` snapshot of the documents whose `document_id` is
  `anchor.pdf.document_id`.
- Capability: `"document_feedback_v1"` in `/v1/health` `capabilities`. Clients keep their
  operations queued, and do not upload them, until the server advertises it. They create and
  show feedback locally before that.
- No render impact: creating or editing feedback never triggers a build.

```json
{
  "schema_version": 1,
  "feedback_id": "9d2f4a51-7c0e-4b8a-a6f1-3e5b2c7d8e90",
  "polarity": "positive",
  "comment": "",
  "anchor": {
    "version": 1,
    "quote": {
      "exact": "The picture draws each route in the plane of the two weights",
      "prefix": "",
      "suffix": ": the horizontal position is a(t)"
    },
    "pdf": {
      "document_id": "c1e62ecc-2dfb-4259-97cc-e319b1186848",
      "pdf_sha256": "f4c8827551095a63ce58c6b1e2c5946014b7b3f62d0f96a780a2931f601a561e",
      "regions": [
        {"page": 1, "rect": [0.0330, 0.5090, 0.8930, 0.5200]}
      ]
    }
  },
  "revision": 1,
  "created_at": "2026-10-28T08:52:03.512Z",
  "updated_at": "2026-10-28T08:52:03.512Z",
  "created_origin_id": "android-cph2719",
  "origin_id": "android-cph2719",
  "deleted": false
}
```

Fields are those of RCF §2 without `card_id`, with these differences:

- `anchor.pdf` is required and has 1–200 `regions` (the selection's line rectangles, RCF §3
  convention).
- `view` may be absent or `"pdf"`, or `"html_attachment"` for feedback made in an HTML attachment
  (§8). Feedback made in the HTML edition view is not supported.
- `segment_id` / `segment_content_sha256` may be set when the client knows the source segment
  of the passage; they are informational, as in RCF §3.
- A payload that carries `card_id` is refused: card feedback is `review_card_feedback`.
- A `review_version` stamp (protocol/README.md) may be added as for any feedback record.

Server validation gives HTTP 422 for the whole batch otherwise, with the RCF §2 rules for the
shared fields.

## 2. The snapshot

`anchor.pdf.pdf_sha256` names the PDF bytes the user was reading. PDF retention keeps that PDF
for as long as the record exists, deleted or not, exactly as for card feedback. So every
feedback record can always be shown on the version it was made on, even after the document is
rebuilt, moved or removed from its project.

## 3. Merge semantics (server, normative)

As RCF §5. The immutable fields are `anchor`, `created_at`, `created_origin_id` and `view`.
Delete is sticky in every arrival order, and Undo is a write with revision + 1.

Removing the document from its project does not tombstone its feedback.

## 4. Finding the passage again (clients, at display time)

As RCF §4, with the document instead of the card's answer as the search domain:

1. **Same PDF**: if `anchor.pdf.pdf_sha256` equals the SHA-256 of the displayed PDF, draw
   `anchor.pdf.regions` as they are. State `exact`.
2. **Otherwise** search the quote (RCF §4.2 skeleton match, hyphen retry and prefix/suffix
   scoring) in the text of the anchor's pages in the displayed PDF, then, if there is no hit,
   in the text of the pages up to 3 before and after them. The page text is every character
   of the page in the platform's reading order. A hit is `relocated` and drawn as in RCF §4.2
   step 6. No hit is `orphaned`.

The result is never written back.

## 5. Client UI rules

### 5.1 Creating

- In normal reading (no review card under review, or the selection does not qualify for card
  feedback), every text selection on a Library PDF with a known `document_id` adds **Like**,
  **Dislike** and **Comment…** at the start of the selection toolbar.
- When a review card's answer is visible and the selection qualifies for card feedback (RCF
  §6.2), the actions create card feedback instead. One selection never creates both.
- Like and Dislike create the record at once with an empty comment, clear the selection and
  draw the highlight. Comment… opens the editor with Like preselected; nothing is saved until
  Save.

### 5.2 Showing, editing, deleting

- Highlights are drawn in normal reading on every page of the document, with the RCF §6.3
  colours, underline styles and comment dot. They are not drawn while a review answer is masked
  or revealed (card feedback is shown then), in exports or printing, and never written into the
  PDF.
- A tap on a highlight opens the RCF §6.3 editor (quote, Like / Dislike, comment, dates, Save,
  Delete, Cancel). Delete writes the tombstone at once and offers Undo for 5 s.
- Orphaned records are not drawn. They are kept and never deleted automatically.

## 6. Agent exposure

The Thoughts connector's `document_feedback` tool lists a document's non-deleted records in
`created_at` order:

```json
{
  "feedback_id": "9d2f4a51-7c0e-4b8a-a6f1-3e5b2c7d8e90",
  "polarity": "positive",
  "quote": "The picture draws each route in the plane of the two weights",
  "comment": "",
  "page": 1,
  "pdf_sha256": "f4c8…",
  "snapshot": "current",
  "created_at": "2026-10-28T08:52:03.512Z",
  "updated_at": "2026-10-28T08:52:03.512Z",
  "origin": "android-cph2719"
}
```

`page` is the page of the first anchor region in that snapshot. `snapshot` is `current` when
`pdf_sha256` is the document's current PDF and `earlier` otherwise.

## 7. Conformance tests

Server (`tests/test_document_feedback.py`):

1. The capability is advertised; a record syncs through `/v1/changes` and `/v1/projections`,
   unknown fields are kept, and a retry is a duplicate.
2. Higher revision wins; equal revisions by `updated_at`; stale revisions are superseded.
3. Delete is sticky against an equal-revision edit in both arrival orders; Undo with
   revision + 1 restores.
4. `anchor`, `created_at` and `created_origin_id` stay those of the first write.
5. Invalid fields give 422: a `card_id`, no `anchor.pdf`, no region, `view: "html"`, bad
   polarity, bad timestamps, inconsistent `deleted`; for `view: "html_attachment"` regions, or no
   valid `anchor.html_attachment`.
6. `/v1/document-sync` carries exactly the non-deleted feedback whose `anchor.pdf.document_id`
   is the target document.
7. PDF retention keeps the PDF named by `anchor.pdf.pdf_sha256`, also for a deleted record.
8. The record has `render_impact` `none` and is not in the build review snapshot.
9. The capability `document_feedback_html_attachment_v1` is advertised; a record made in an HTML
   attachment syncs, and retention keeps its attachment object, also for a deleted record.

10. The capability `document_feedback_kinds_v1` is advertised; a question syncs with its
    `kind`, `polarity: "neutral"` and later `resolved_at`; `neutral` without a `kind`, a `kind`
    with another polarity, and `resolved_at` on a like, a dislike or with a bad time give 422.
11. The capability `document_feedback_drawing_v1` is advertised; a drawing syncs and its strokes
    change with later revisions while its anchor does not; `kind: "drawing"` without strokes,
    strokes without that kind, a point outside the unit square, an empty stroke, `resolved_at`
    on a drawing, and a drawing in an HTML attachment give 422.
12. The capability `document_feedback_text_v1` is advertised; a text box syncs and its text and
    place change with later revisions while its anchor does not; `kind: "text"` without
    `text_box`, `text_box` without that kind, an empty or blank comment, a rect outside the unit
    square or with no width or height, a font size out of range, `resolved_at` on a text box, and
    a text box in an HTML attachment give 422.

Android: payload round-trip with unknown fields, the capability gate, local merge and sticky
delete, create/edit/delete/undo revisions, and an instrumented check that a highlight is drawn
in normal reading and not during a review.

## 8. Feedback in HTML attachments

Capability: `"document_feedback_html_attachment_v1"` in `/v1/health` `capabilities`. Clients keep
these operations queued, and do not upload them, until the server advertises it: an older server
refuses the whole batch.

An HTML attachment (`thoughts-html:` link of a document's PDF) has no PDF geometry, and its text
is often generated by its scripts. So, as for review cards made in an attachment, the quote and
the page state are the anchor:

```json
{
  "view": "html_attachment",
  "anchor": {
    "version": 1,
    "quote": {"exact": "The item is easy for this agent", "prefix": "", "suffix": ""},
    "pdf": {
      "document_id": "c1e62ecc-2dfb-4259-97cc-e319b1186848",
      "pdf_sha256": "f4c8827551095a63ce58c6b1e2c5946014b7b3f62d0f96a780a2931f601a561e",
      "regions": []
    },
    "html_attachment": {"path": "docs-irt/html/item_response.html", "sha256": "ab12…"},
    "page_state": {"hash": "#curves"}
  }
}
```

The other fields, the merge rules and §2 are as above.

- `anchor.pdf` names the document and the PDF that linked the page; `regions` is empty.
- `anchor.html_attachment` is required: the logical `path` and the `sha256` of the page. Retention
  keeps that object as long as the record exists, deleted or not.
- `anchor.page_state.hash` is the page's `location.hash` when the passage was marked, or empty.
- Clients find the passage in the page by its quote, after restoring the page state. No hit is
  `orphaned`: the record is kept and not drawn.
- A client that does not show these records skips them. They are never drawn on the PDF.
- The connector's `document_feedback` tool lists them with `page: null` and
  `html_attachment: {path, page_state}`.

## 9. Comment kinds and versions

Capability: `"document_feedback_kinds_v1"` in `/v1/health` `capabilities`. Clients keep records with
a `kind` queued, and do not upload them, until the server advertises it: an older server refuses
the whole batch.

The user sees one thing, a comment on a passage, of one of four kinds:

| Kind | Record | Shown |
| --- | --- | --- |
| Like | `polarity: "positive"`, no `kind` | on the version it was made on |
| Dislike | `polarity: "negative"`, no `kind` | on the version it was made on |
| Question | `polarity: "neutral"`, `kind: "question"` | until resolved |
| Other | `polarity: "neutral"`, `kind: "other"` | until resolved |

`polarity: "neutral"` and `kind` come together. `kind` and `polarity` may change with an edit, so a
comment can change kind. `resolved_at` (RFC 3339) closes a question or another comment and can be
removed again by an edit; a like or a dislike has none.

**Versions.** A like or a dislike is an opinion of what was on the page: it is drawn and listed only
while the displayed PDF is the one it was made on (`anchor.pdf.pdf_sha256`; in an HTML attachment
`anchor.html_attachment.sha256`), so section 4 does not relocate it. A question or another comment
asks for something, so it follows the text as in section 4, on every version, until it is resolved.

**Nothing is lost.** Records that are hidden this way stay on the server. Clients offer them
behind one line ("N from earlier versions"), and agents read all of them with their snapshot.

Clients that do not know this section drop records with `polarity: "neutral"` when they parse them,
and keep showing likes and dislikes as in section 4.

## 10. Drawings

Capability: `"document_feedback_drawing_v1"` in `/v1/health` `capabilities`. Clients keep drawings
queued, and do not upload them, until the server advertises it: an older server refuses the whole
batch. They show them locally before that.

A drawing is pencil strokes on a page of the PDF, made with the mouse or a pen while reading.

| Kind | Record | Shown |
| --- | --- | --- |
| Drawing | `polarity: "neutral"`, `kind: "drawing"`, `drawing` | on the version it was drawn on |

```json
{
  "polarity": "neutral",
  "kind": "drawing",
  "comment": "",
  "anchor": {
    "version": 1,
    "quote": {"exact": "Drawing", "prefix": "", "suffix": ""},
    "pdf": {"document_id": "…", "pdf_sha256": "…", "regions": [{"page": 3, "rect": [0, 0, 1, 1]}]}
  },
  "drawing": {"strokes": [{"points": [[0.2104, 0.3312], [0.2231, 0.3398]]}]}
}
```

- `drawing.strokes`: 1 to 200 strokes, oldest first, each with `points`: 1 to 5000 `[x, y]` in
  normalized page coordinates, with the origin at the top left, as in regions. One point is a dot.
  A client may start another drawing when one is full.
- `anchor.pdf.regions`: one region, the whole page. The anchor is kept as first written (section 3)
  and a drawing grows, so the strokes say where it is and the anchor only names the page.
- `anchor.quote.exact`: `"Drawing"`, because a quote is required. Nothing searches for it.
- `comment` is optional, as for a like. `view` is `"pdf"`; a drawing has no `resolved_at`.

**Versions.** A drawing is made over what was on the page, so like a like it shows only while the
displayed PDF is `anchor.pdf.pdf_sha256`. It is never relocated, because there is no passage to find.
Clients list it behind "N from earlier versions" as in section 9, and do not draw it there.

Clients that do not know this section drop drawings as in section 9. Agents read a drawing as one on
its page, without its points.

## 11. Text boxes

Capability: `"document_feedback_text_v1"` in `/v1/health` `capabilities`. Clients keep text boxes
queued, and do not upload them, until the server advertises it: an older server refuses the whole
batch. They show them locally before that.

A text box is text typed on a page of the PDF, where the user clicked, like the Text tool of a PDF
viewer. It can be moved on its page, edited and deleted.

| Kind | Record | Shown |
| --- | --- | --- |
| Text box | `polarity: "neutral"`, `kind: "text"`, `text_box` | on the version it was typed on |

```json
{
  "polarity": "neutral",
  "kind": "text",
  "comment": "Why not the median?",
  "anchor": {
    "version": 1,
    "quote": {"exact": "Text", "prefix": "", "suffix": ""},
    "pdf": {"document_id": "…", "pdf_sha256": "…", "regions": [{"page": 3, "rect": [0, 0, 1, 1]}]}
  },
  "text_box": {"rect": [0.1204, 0.3312, 0.3518, 0.3501], "font_size": 0.0145}
}
```

- `comment` is the text, 1 to 4000 characters and not blank. Line breaks are kept. So a client that
  lists comments lists the text, and agents read it as one.
- `text_box.rect`: `[left, top, right, bottom]` in normalized page coordinates, with the origin at
  the top left, as in regions. The text starts at the top left and wraps at the width of the rect;
  the height is that of the text as the writing client laid it out. A client whose font is wider or
  taller may draw outside the rect: the text matters, not the box. A move changes the rect.
- `text_box.font_size`: the height of the font as a fraction of the page's height, 0.004 to 0.2, so
  the text keeps its size on the page at every zoom. A client draws it in its own sans-serif font.
- `anchor.pdf.regions`: one region, the whole page, as for a drawing (section 10). The anchor is kept
  as first written (section 3) and a text box moves, so `text_box` says where it is and the anchor
  only names the page. A text box stays on its page.
- `anchor.quote.exact`: `"Text"`, because a quote is required. Nothing searches for it.
- `view` is `"pdf"`; a text box has no `resolved_at`.

**Versions.** A text box is written over what was on the page, so like a drawing it shows only while
the displayed PDF is `anchor.pdf.pdf_sha256`, is never relocated, and is listed behind "N from earlier
versions" as in section 9 without being drawn there.

Clients that do not know this section drop text boxes as in section 9. Agents read a text box as its
text on its page, without its place.
