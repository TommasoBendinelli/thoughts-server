<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Merging review cards on the server (capability `review_card_merge_v1`)

The desktop merges review cards of one document into one card (Merge cards…, `thoughtsctl
merge-review-cards`) and sends the result through its outbox as ordinary records. A client without
that outbox, such as a remote agent connector or `thoughts-cli`, asks the server for the same merge:

```
POST /v1/review-card-merges
{"request_id": "<fresh UUID>", "card_ids": ["<card UUID>", ...], "keep_card_id": "<one of card_ids>",
 "question": "<merged question>", "source": "<source path>", "expected_source_sha256": "<64 hex>"}
```

`keep_card_id` and `question` are optional. `card_ids` holds 2 to 20 different cards.

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

## Refusals

A refusal changes nothing and answers `{"accepted": false, "error_code": ..., "detail": ...}`:

| Status | `error_code` | Meaning |
|---|---|---|
| 422 | `invalid_request` | Not 2 to 20 different card UUIDs, a `keep_card_id` outside them, no request UUID or source SHA-256, a question outside 1 to 500 characters, or an unknown field. |
| 409 | `card_unavailable` | A card does not exist or was deleted. |
| 409 | `invalid_source` | The source does not exist, is too large, or is not the file the cards' passages are in. |
| 409 | `source_conflict` | The source is not at `expected_source_sha256`; `current_source_sha256` names its version. Read it again. |
| 409 | `merge_refused` | The cards cannot be merged; `detail` says why: cards of several documents or source files, a card not marked in the source (a PDF-only card), cards on different PDF versions, a card without answer areas yet, markers that are incomplete, nested or not on lines of their own, a card whose record lists other passages than its markers hold. |
| 409 | `request_conflict` | The request ID was used before with other arguments. |

## Retries

The records' identities derive from `request_id`. A request whose kept-card record already exists
answers that first result again with `replayed: true`, also after later changes to the cards; the
same request ID with other arguments is refused with `request_conflict`.

## Answer

`200`:

```json
{"schema_version": 1, "accepted": true, "terminal": true, "state": "merged", "replayed": false,
 "request_id": "...", "kept_card_id": "...", "deleted_card_ids": ["..."], "delete_event_ids": ["..."],
 "card_order": ["...", "..."], "question": "...", "source": "docs-x/tex/source/main.tex",
 "source_sha256": "<the merged source>", "passages": 2, "card_revision": 3, "document": "x.pdf",
 "message": "..."}
```

`card_order` lists every merged card in reading order (by its first passage).
