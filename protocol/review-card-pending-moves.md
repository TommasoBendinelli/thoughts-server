<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Card moves that wait for an agent proposal (capability `review_card_pending_moves_v1`)

An agent moves a review card without source markers (a card on an imported PDF) into a note with
`move-review-card` (`thoughts-cli`, the connector's `move_review_card`): the card keeps its ID,
question, schedule and history, and its markers go around the selected lines. A selection inside a
pending agent proposal is refused, because proposed text is only an offer until the user accepts it.

With this capability, a move onto lines of one pending proposal's **proposed text** waits for the
proposal instead. The server records it and makes it itself when the user accepts the proposal:

```
POST /v1/review-card-pending-moves
{"request_id": "<fresh UUID>", "card_id": "<card UUID>", "source": "<the note's main source>",
 "expected_source_sha256": "<64 hex>", "start_line": 9, "end_line": 11}
```

The fields are `move-review-card`'s. `start_line` and `end_line` (one-based, end exclusive) are lines
of the version read, with its wrappers.

## The selection

The lines must lie inside the proposed text (the replacement) of exactly one valid pending proposal:
in LaTeX the last argument of `\agentProposedEdit`, in Markdown the text between a proposal's header
comment and its end comment. They may also hold the wrapper's own syntax around that text (the
`}{` before it on its first line, the closing `}` or the end comment after it, with nothing else on
that line), never its explanation, its original text or text outside the wrapper.

The server reads the selection in the source as it will be once that proposal alone is accepted
(protocol/agent-proposals.md section 7): the part of the selection that is proposed text maps to
whole lines of the accepted source, which must all be proposed text (the LF right after proposed text
that does not end in one counts as its own: a proposal beside a review card marker keeps its last LF
outside the wrapper, agent-proposals.md 8.1), and the ordinary selection rules
of `move-review-card` apply there (`review_card_selection.py`, the desktop's `sourceSelectionError`:
complete lines with text, outside other pending proposals and other cards' passages, LaTeX groups and
environments closed, Markdown blocks whole). Those lines are the move's **target**; the answer shows
them as `target_text`.

The card must be movable as for `move-review-card`: an approved card placed by areas, without source
markers, of another document than the note. The note must be the main source of exactly one Library
document with a PDF. A card waits for at most one proposal, and two waiting moves never share lines.

## Answer

`200`, and nothing changed yet:

```json
{"schema_version": 1, "accepted": true, "terminal": false, "state": "waiting_for_proposal",
 "replayed": false, "pending_move_id": "<request_id>", "request_id": "...", "card_id": "...",
 "proposal_id": "agent-...", "source": "docs-x/tex/source/main.tex", "source_sha256": "<unchanged>",
 "document": "x.pdf", "document_id": "...", "from_document": "lecture.pdf",
 "target_text": "Which two implementation techniques ...?\n", "message": "..."}
```

A request whose ID is already recorded answers that first result again with `replayed: true` and
`pending_move` (the move's current state, below), also after the proposal was decided; the same ID
with other arguments is refused with `request_conflict`.

## When the proposal is decided

Every verified source change reconciles the current proposal state
(`proposal_history.reconcile_proposal_history`): a client's publication or source save, a conflict
resolution, a server-side resolution (the phone's Accept), a document move. Waiting moves follow
their proposal in the same transaction:

| The proposal | The move |
|---|---|
| accepted (also `decided_in_parts`) | **made**: state `moved` (below) |
| rejected, withdrawn, dismissed, deleted with its document | dropped: `proposal_rejected`, `proposal_withdrawn`, `proposal_dismissed`, `proposal_document_deleted`; the card stays where it was |
| revised (`changed`) | follows the target when its text still stands, line-aligned, in the new proposed text and the selection rules accept it there; otherwise dropped, `proposal_revised` |
| moved to another file, or restored | follows it the same way |
| removed outside Thoughts (`removed_outside`) | keeps waiting: a late decision claim, a Restore or a Dismiss settles it |

A move is also dropped when its card is deleted (`card_deleted`), moved to another document
(`card_moved`) or linked to source markers (`card_linked`) another way first; a client operation that
does so drops it at once, and a read already shows such a move as dropped.

### The move

The server moves the card exactly as `move-review-card` would move it onto the accepted text. It
finds the target in the version that decided the proposal: where the proposed text went when that
version is the previous one with only this proposal accepted; when the same upload also edited the
note, the target's lines follow that edit as markers follow an edit (below); otherwise, or when an
edit cut across the target, the one line-aligned occurrence of the target's text outside pending
proposals, the nearest to the proposal's place (none, or a tie, drops the move: `text_changed`).
Every target of the decided proposal is found first and the cards are marked from the end of the file
up. The selection rules are checked again there (`selection_refused`). Then:

* the card's marker pair goes around the target lines (`\ReviewCardMaskStart{card}{0}%` /
  `\ReviewCardMaskEnd{card}{0}%`, Markdown `<!-- thoughts:v1:start review:card:answer:0 -->` / `end`),
  each on a line of its own;
* the card record is the desktop's moved card (`review_card_selection.moved_card`): same ID, question,
  schedule and history; `primary` names the note's document and the new version-2 `source_anchor`;
  the old placement is dropped; `move` names both PDF stems; `move_request` holds the pending move's
  receipt; `reanchor` is `pdf_placement_pending`;
* it is sent as one client-v1 `review_card` operation with its `source_change`, as the client would
  send it, and checked the same way (`review_card_move.validate_card_move`, `client_review_sources.py`).
  The records belong to the server device `thoughts-review-card-pending-moves`.

The move runs in a savepoint of the transaction that stored the deciding version. If it fails, it is
rolled back and dropped (`move_failed`, with the reason in `message`); the user's change always
stands. A retry of the deciding request (the same publication or operation ID) is answered from its
record and never moves the card twice. The card's PDF areas are placed by the next build, which the
next Sync asks for.

### The device that accepted

The deciding device stores the accepted version; the marked one follows in the same transaction, so
the device's answer names the accepted version and its next Sync receives the marked one. If the
user edited the note again in between, that upload's base is the accepted version and the server
holds it with the card's markers. Markers a pending move placed are not text: the merge
(`pending_card_moves.with_moved_card_markers`, used by `sources._merge`) merges the two versions as
if they were not there and puts them back around the same passage of the result, so the card follows
the edit instead of a Merge needed. Lines inserted right before or after the passage stay outside it;
an edit that cuts across a boundary of the passage, or removes it, keeps the conflict. Markers placed
any other way merge exactly as before.

## Reading and cancelling

`GET /v1/review-card-pending-moves?card_id=&proposal_id=&state=` lists moves, newest first (at most
`limit`, 100 by default, 200 at most); `state` is `waiting_for_proposal` (default), `moved`,
`dropped`, `cancelled` or `all`. `GET /v1/review-card-pending-moves/{pending_move_id}` answers one:

```json
{"pending_move_id": "...", "request_id": "...", "card_id": "...", "proposal_id": "agent-...",
 "source": "...", "state": "moved", "document": "x.pdf", "document_id": "...",
 "from_document": "lecture.pdf", "target_text": "...", "created_at": "...", "updated_at": "...",
 "settled_at": "...", "message": "...",
 "result": {"state": "card_moved", "source_sha256": "<the marked source>", "card_revision": 2, "...": "..."}}
```

A settled move also has `reason` (above). `POST /v1/review-card-pending-moves/{pending_move_id}/cancel`
cancels a waiting move (`state: cancelled`, `reason: cancelled`); a move already dropped or cancelled
answers its state with `already_settled: true`; a move already made is refused with `already_moved`.

## Refusals

A refusal records nothing and answers `{"accepted": false, "error_code": ..., "detail": ...}`:

| Status | `error_code` | Meaning |
|---|---|---|
| 422 | `invalid_request` | A field is missing or malformed, or unknown. |
| 409 | `not_pending` | The lines touch no pending proposal: use the ordinary move. |
| 409 | `invalid_selection` | The lines are not (only) one proposal's proposed text, or the accepted text fails the selection rules; `detail` says why. |
| 409 | `source_conflict` | The source is not at `expected_source_sha256`; `current_source_sha256` names its version. |
| 409 | `invalid_source` | The source does not exist, is not UTF-8, holds NUL or exceeds 4 MiB. |
| 409 | `card_unavailable`, `unsupported_card`, `linked_card` | The card cannot move this way (deleted, not approved, not placed by areas, already linked to source markers: such a card moves with its marked passage, agent-proposals.md section 12). |
| 409 | `document_unavailable`, `same_document` | The source is not one Library document's main source with a PDF, or the card already belongs to it. |
| 409 | `pending_move_exists` | The card already waits for a proposal; `pending_move_id` names that move. |
| 409 | `target_taken` | Another card already waits to be moved onto some of these lines; `pending_move_id` names it. |
| 409 | `request_conflict` | The request ID was used before with other arguments. |

## Clients

`thoughts-cli` 1.6.0 sends this request from `move-review-card` when the lines are one pending
proposal's proposed text and the server has the capability (otherwise it refuses the selection as
before), and adds `pending-card-moves` and `cancel-pending-card-move`. The connector's
`move_review_card` reports `waiting_for_proposal`; `pending_card_moves`, `cancel_pending_card_move`
and `get_review_card` (`pending_moves`) show and cancel such moves. Creating a new card on pending
text stays refused.
