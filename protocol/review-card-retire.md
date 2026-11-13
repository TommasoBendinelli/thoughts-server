<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Retiring review cards

The user retires a review card when it should never be offered for review again but its history must stay.
Delete is permanent: the card, its history and its feedback disappear from every view and its source markers are removed.
Retire is reversible: "Restore to review" makes the card active again with its old schedule.

Status: server `review_card_retire_v1` (branch feature/retire-card). Desktop and Android implement §4–§7.
Conformance cases: `fixtures/review-card-retire.json`. Clients keep a copy and test their fold against it.

## 1. Transport

Retire, restore and their undo are `review_event` records (collection `events`), like answers and skips.
They are queue actions: the event carries `queue_action` and no `rating`.
Review events are immutable on the server (the first write of an `event_id` wins, later writes are `superseded`).
No new endpoint, record type or migration is needed.

Clients may upload these events to any server version.
A server without `review_card_retire_v1` stores and serves them unchanged, because review events are free-form and queue actions are never ratings (`review_reanchor._deleted_review_card_ids` ignores them).
The capability says that the server validates them (§3) and knows retired cards (§6).

## 2. Events

All three share `schema_version: 3`, a random lowercase UUIDv4 `event_id` (equal to the operation's `entity_id`), `card_id` (lowercase UUID), `reviewed_at` (UTC ISO 8601, the moment of the action on the device) and `origin_id` (the device).
A `review_version` stamp (protocol/README.md) may be added as for any feedback record.

- Retire: `"queue_action": "retire"`.
- Restore to review: `"queue_action": "restore"`.
- Undo of a retire or a restore: `"queue_action": "undo_retire"` and `"undone_event_id": "<event_id of that retire or restore>"`.

Example:

```json
{"schema_version": 3, "event_id": "6f1c…", "card_id": "79cf49c9-…", "queue_action": "retire",
 "reviewed_at": "2026-10-27T14:02:11.204Z", "origin_id": "android-…"}
```

The events carry no `rating`, `review_tags`, `skip_id`, `skip_until`, `interval_action` or `retracted_event_id`.
Older clients therefore never read them as answers, skips or retractions (§8).

## 3. Server validation

With `review_card_retire_v1` the server refuses an operation batch (HTTP 409, nothing stored) containing a review event whose `queue_action` is `retire`, `restore` or `undo_retire` when:

- `schema_version` is not 3, or `card_id` is not a UUID, or `reviewed_at` is not an ISO 8601 time;
- it also carries `rating`, `retracted_event_id` or `interval_action`;
- an `undo_retire` lacks a UUID `undone_event_id`.

The server does not check that the card exists, is approved or is not deleted, and it does not check the target of an undo: events may arrive in any order (§4).

## 4. Fold: the retired state of a card

The state is a pure function of the set of the card's review events, so arrival order does not matter.

1. Deletion wins. A card with a deliberate delete event (`rating: "delete"` that is not an inferred source deletion, as `_deleted_review_card_ids` defines it) is deleted, whatever its retire events say and in whichever order they arrived.
2. Valid actions: events of the card with `queue_action` `retire` or `restore`, a parseable `reviewed_at`, and `card_id` equal to the card.
3. Undo: an `undo_retire` event cancels the action whose `event_id` is its `undone_event_id`, if that action belongs to the same card. An undo that names an answer, a skip, another card's event or an unknown event does nothing. An undo that arrives before its target cancels the target when it arrives.
4. The card is retired when the latest valid action that is not cancelled, ordered by `reviewed_at` and then by `event_id` (the order of review history), is a `retire`. Otherwise it is active.
5. `retired_at` is the `reviewed_at` of that retire event, and `retire_event_id` its `event_id`.

This is last-writer-wins by action time, the same order the clients use to fold answers.
Retiring an already retired card or restoring an active one is allowed and harmless.
A retired card that is deleted later is deleted; there is no way back from deletion, as today.

## 5. What retirement changes

Retire and restore never change the schedule.
The answer fold ignores queue actions, so the card keeps its interval, due date, review and lapse counts.
After a restore, a due date in the past simply makes the card due now.

A retired card:

- is not offered for review: excluded from library-wide and per-document review sessions, the next-card choice, due and overdue counts, "Skip all due" targets and Today summaries;
- is not counted as an active card in the Library review-card columns (clients count only active cards; deleted cards were already excluded);
- keeps its review history, tags, group notes and answer note, its answer feedback, its source markers and answer location;
- stays in re-anchoring and source linking on the server (its markers and PDF regions follow the source as before);
- is shown in History and the card's details with the state "Retired <date>", and in the card browser under the state/filter Retired;
- can be answered from an older client that does not know retirement (§8). Such an answer is recorded as usual and does not restore the card.

## 6. Server

The server keeps no state table for retirement: it reads the events.
`review_retirement.retired_review_cards(events)` implements §4 and `retired_review_card_ids(session)` reads it from the stored events.
Re-anchoring and source linking treat retired cards as live cards; only `_deleted_review_card_ids` stops them.
Document deletion writes its usual delete events for retired cards too.

## 7. Clients

- Review card menu (⋮): "Retire card" directly above "Delete card". It writes a retire event and takes the card out of the current session like Delete. The session's Undo writes `undo_retire` for that event and brings the card back.
- Wherever a retired card is shown (card details, card browser, History panel): "Restore to review" instead of "Retire card". Restore writes a restore event.
- Delete's confirmation also says that Retire keeps the history.
- History lists the events as "Retired", "Restored to review" and "Undo retire" with their time.
- Agent tools (MCP `search_review_cards`, `knowledge_profile`, the claude.ai connector) report retired cards with status `retired` and `retired_at`. Deleted cards stay excluded. `knowledge_profile` keeps using a retired card's history.

## 8. Older clients

An older client ignores the three events when it folds answers and skips:
Android before feature/retire-card reads an unknown `queue_action` as neither answer nor skip, and the desktop's `foldEvents` and skip fold skip every `queue_action` event.
Nothing is lost: an older client keeps the events and uploads nothing in their place.
What it shows: the card stays active and due there, and can be reviewed there.
Its History lists a retire or restore as a plain "Review" entry on Android and does not list it on the desktop.
Once updated, the client shows the card as retired again, because the state comes from the events.
