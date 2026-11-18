<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Merging review cards on the server (capabilities `review_card_merge_v1`, `review_card_merge_pdf_v1`)

The desktop merges review cards of one document into one card (Merge cards…, `thoughtsctl
merge-review-cards`) and sends the result through its outbox as ordinary records. A client without
that outbox, such as a remote agent connector or `thoughts-cli`, asks the server for the same merge:

```
POST /v1/review-card-merges
{"request_id": "<fresh UUID>", "card_ids": ["<card UUID>", ...], "keep_card_id": "<one of card_ids>",
 "question": "<merged question>", "source": "<source path>", "expected_source_sha256": "<64 hex>"}
```

`keep_card_id` and `question` are optional. `card_ids` holds 2 to 20 different cards.

Cards of an imported PDF (no Thoughts source behind them: no `primary.source_anchor.segments`) are
merged with the same request without `source` and `expected_source_sha256` (capability
`review_card_merge_pdf_v1`; see "Cards of a PDF" below). A request with only one of the two fields
is refused (`invalid_request`).

## What changes

The kept card keeps its ID, schedule and review history and takes the merged question. Every card's
marked passage becomes a passage of the kept card, its own segment, numbered `0..n-1` in file order;
text between passages (figures, other paragraphs) stays unmarked; passages that overlap become one.
The other cards are deleted with their history.

The server makes exactly the records the desktop's merge sends and applies them as one batch, so the
canonical source and every record change in one database transaction, or nothing changes:

1. For each other card, in reading order, a `review_event` deletion (`rating: "delete"`,
   `merged_into: <kept card id>`, `marker_removal: "complete"`, `source_authority: "client-v1"`) whose
   `source_change` removes that card's markers from the version the previous step left, exactly as
   deleting the card removes them (`delete_marked_passages(delete_text=False)`: each marker with its
   line, the text kept).
2. The kept card (revision + 1, `schema_version` 4, `source_authority: "client-v1"`) whose
   `source_change` moves its markers: the source without its old markers equals the source without
   its new ones (`client_review_source_replacement_v1`). Its `primary.source_anchor` is the new
   version-2 anchor; `primary.regions` are each card's answer areas in reading order (the next build
   places them again); `primary.answer_anchor` joins the cards' answer texts; `merge_request` holds the
   request's receipt.

Each step is checked as any client-v1 card operation is (`client_review_sources.py`); every
`source_change` names the source version the merge started from as `base_sha256`. Other devices
receive the records like a merge made on the Mac. The records belong to the server device
`thoughts-review-card-merge`. The merge does not request a build; the caller asks for one
(`POST /v1/render-checks`), as after any card change.

When `keep_card_id` is absent, the card with the most reviews is kept (the first in reading order on
a tie). When `question` is absent, the cards' questions are joined with a space in reading order,
each once.

## Cards of a PDF

When no selected card is marked in a source, and every card is an approved card answered on the PDF
(`card_type` absent or `visual_region`; not a whole-document card, `primary.whole_document` or
`primary.scope: "document"`; not a generated `basic_*` card) of one document, placed on one
`primary.pdf_sha256`, each with valid answer areas (`primary.regions`, or `page` and `rect`; an
`answer_region_override` on that PDF wins), the request without source fields merges them on the PDF.
Nothing in a source or in the PDF changes.

Reading order is the order of each card's first area: the smallest (page, top, left) among its
areas; ties keep the request's order. The default kept card and question follow the rules above.

1. For each other card, in reading order, a `review_event` deletion as the desktop's Delete card
   writes it, naming the kept card: `schema_version` 3, `event_id`, `card_id`, `reviewed_at` and
   `updated_at` (after the card's own events), `origin_id`, `revision` 1, `rating: "delete"`,
   `scheduler_version` 2, `source_anchor` (the card's `primary.source_anchor`, `{}` when it has none),
   `merged_into`. No `source_authority`, `marker_removal` or `source_change`: there are no markers
   to remove.
2. The kept card (revision + 1, `schema_version` 4): `primary.question` the merged question;
   `primary.regions` every card's own areas, cards in reading order, the first card's areas on its
   own page leading (`primary.page`, or the page of its first corrected area when an
   `answer_region_override` is in effect, as the desktop's `ReviewCard::page`, also for source
   merges); `primary.page` and `primary.rect` the first of them;
   `primary.page_visual_sha256` the first card's, `primary.page_visual_fingerprint_method`
   `poppler-rgb-96-v1`, `primary.pdf_sha256` the cards' PDF; `primary.answer_anchor` the cards'
   answer texts joined with a space in reading order (removed when none has one).
   `primary.answer_region_override`, `primary.source_kind`, `primary.component_origin`, `reanchor`
   and `source_change` are removed; `source_anchor`, `source_link`, `source_authority` and
   `review_version` stay as they were; `needs_reanchor` becomes `approved`.

Both are applied as one batch, locked on the document (`pdf:<pdf_stem>.pdf`) and on the merge
device's operation numbers. Cards marked in a note and cards only on the PDF are not merged together
(`merge_refused`: "Card “A” is marked in its note and card “B” is only on the PDF. ..."); a request
with source fields for cards of a PDF is refused as "not linked to a source passage", saying to leave
both fields out, and a request without them for cards marked in a note says to give them.

## Refusals

A refusal changes nothing and answers `{"accepted": false, "error_code": ..., "detail": ...}`:

| Status | `error_code` | Meaning |
|---|---|---|
| 422 | `invalid_request` | Not 2 to 20 different card UUIDs, a `keep_card_id` outside them, no request UUID, only one of `source` and `expected_source_sha256` or an invalid one, a question outside 1 to 500 characters, or an unknown field. |
| 409 | `card_unavailable` | A card does not exist or was deleted. |
| 409 | `invalid_source` | The source does not exist, is too large, or is not the file the cards' passages are in. |
| 409 | `source_conflict` | The source is not at `expected_source_sha256`; `current_source_sha256` names its version. Read it again. |
| 409 | `merge_refused` | The cards cannot be merged; `detail` says why: cards of several documents or source files, a card not marked in the source (a PDF-only card) in a request with source fields, cards marked in a note and cards only on the PDF together, cards on different PDF versions, a card without answer areas (yet), a whole-document or generated card, markers that are incomplete, nested or not on lines of their own, a card whose record lists other passages than its markers hold. |
| 409 | `request_conflict` | The request ID was used before with other arguments. |

## Retries

The records' identities derive from `request_id`. A request whose kept-card record already exists
answers that first result again with `replayed: true`, also after later changes to the cards; the
same request ID with other arguments is refused with `request_conflict`.

## Answer

`200`:

```json
{"schema_version": 1, "accepted": true, "terminal": true, "state": "merged", "kind": "source", "replayed": false,
 "request_id": "...", "kept_card_id": "...", "deleted_card_ids": ["..."], "delete_event_ids": ["..."],
 "card_order": ["...", "..."], "question": "...", "source": "docs-x/tex/source/main.tex",
 "source_sha256": "<the merged source>", "passages": 2, "card_revision": 3, "document": "x.pdf",
 "message": "..."}
```

`card_order` lists every merged card in reading order (by its first passage). `kind` is `"source"`.

For cards of a PDF the answer has `"kind": "pdf"`, `"source": null`, `"source_sha256": null` and
`passages` = the number of the kept card's areas; its message says nothing about a build, since none
is needed.
