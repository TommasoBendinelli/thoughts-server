<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Knowledge cards and project cards

Every review card is one of two kinds.

- **Knowledge cards** are material the user studies. They keep today's spaced repetition.
- **Project cards** are about work the user is writing or building, so their content keeps changing.
  They come back at least once a week, come back at once when their passage changes, and can be retired together when the project ends.

A card follows its Library Project's kind unless the user set the card's kind by hand.
Every Project and every card is a knowledge card until someone chooses otherwise, so existing cards keep their behaviour.

Status: server `review_card_kinds_v1` (branch feature/review-card-kinds). Desktop, Android, thoughtsctl, the desktop MCP tools, thoughts-cli and the Library connector implement §3–§9.
Conformance cases: `fixtures/review-card-kinds.json`. Clients keep a copy and test their folds against it.

## 1. Values

A kind is the string `"knowledge"` or `"project"`.
Anything else, or no value, never makes a card a project card.

## 2. Records

### 2.1 Kind events (per card, by hand)

Choosing a card's kind is a `review_event` queue action, like Retire (`review-card-retire.md`).
Review events are immutable, so two devices never conflict over a card's kind.

```json
{"schema_version": 3, "event_id": "6f1c…", "card_id": "79cf49c9-…", "queue_action": "set_review_kind",
 "review_kind": "project", "reviewed_at": "2026-11-17T15:02:11.204Z", "origin_id": "android-…"}
```

- `schema_version` 3, a random lowercase UUIDv4 `event_id` (the operation's `entity_id`), `card_id`, `reviewed_at` (UTC ISO 8601, the moment of the choice on the device) and `origin_id`, as for every review event.
- `review_kind` is `"knowledge"` or `"project"` (the card's own kind), or `"follow_project"` (the card has no kind of its own again and follows its Project).
- The event carries no `rating`, `review_tags`, `skip_id`, `skip_until`, `interval_action` or `retracted_event_id`, so no client ever reads it as an answer, a skip or a retraction.
- A `review_version` stamp (README.md) may be added.

There is no undo event: an Undo writes another `set_review_kind` event with the card's previous choice (`follow_project` when it followed its Project).

### 2.2 The kind a card was made with

A `review_card` record may carry `"review_kind": "knowledge" | "project"`: the kind chosen when the card was made (the agent tools' `kind`, §8, or a client's own creation choice).
It counts as the card's own kind until a kind event says otherwise.
Clients that rewrite a card keep it, as they keep every unknown field.
Clients never add or change it to swap a card's kind; a swap is always a kind event.

### 2.3 A Project's kind

The Library-project record (`GET /v1/library/projects`) of every Project has `"review_kind": "knowledge" | "project"`, `"knowledge"` until the user changes it.
The field is present on every Project exactly when the server has `review_card_kinds_v1`, so clients can gate the Project choice on it, also offline.

Clients change it with a Library-project action, like `rename`:

```json
{"operation_id": "<uuid>", "project_id": "<project id>", "base_revision": 7,
 "action": "set_review_kind", "review_kind": "project"}
```

- `review_kind` must be `"knowledge"` or `"project"`; anything else is refused and nothing changes.
- Setting the current value is accepted and changes only the revision.
- The Project manifest (`thoughts-project.json`) is not touched and no card record is written: cards with a kind of their own keep it, the others follow the new value at once.
- Removing a Project removes its kind with it.

## 3. Fold: a card's kind

A card's kind is a pure function of its review events, its record and the catalogue, so arrival order does not matter.

1. **Kind events.** Of the card's review events, the valid kind events are those with `queue_action: "set_review_kind"`, `card_id` equal to the card, a `reviewed_at` that parses as an ISO 8601 time with a zone, and `review_kind` one of `knowledge`, `project`, `follow_project`. The latest one counts, ordered by `reviewed_at` as an instant (not as a string: devices write different precisions) and then by `event_id`.
2. If the latest kind event says `knowledge` or `project`, that is the card's kind; its source is `card`.
3. If it says `follow_project`, go to step 5. The record's `review_kind` (§2.2) no longer counts.
4. With no valid kind event, a record `review_kind` of `knowledge` or `project` is the card's kind; its source is `card`.
5. Otherwise the card follows its Projects (§4): `project` when any Project that holds the card has `review_kind: "project"`, else `knowledge`. The source is `project` when at least one Project holds the card, else `default`.

The kind of a retired card is kept and shown; a deleted card has none.

## 4. A card's Projects

A Project holds a card when, among the Project's `documents` and `unpublished_documents`:

- one has the card's document ID as `document_id`, the card's document ID being `primary.pdf_document_id` if present, else `primary.document_id`; or
- one has the card's PDF file (`primary.pdf_stem` + `".pdf"`) as `filename`, or that file is the Project's `target` (its combined reading PDF).

A card may be held by several Projects (a PDF shared between Projects). It is a project card when any of them is a project Project (§3 step 5), and it is listed under each of them (§7).

## 5. Choosing and swapping

- **Per card.** The card's menu (the review panel's menu, the card browser, the reader's Cards list) has **Make project card** on a knowledge card and **Make knowledge card** on a project card. It writes a kind event with the other kind. When the card has a kind of its own (source `card`) and a Project holds it, the menu also has **Follow the Project (Knowledge)** or **(Project)**, naming the Project's kind, which writes `follow_project`.
- **Per Project.** The Project's menu offers the default kind, **Knowledge cards** or **Project cards**, showing the current one. It sends `set_review_kind` (§2.3), and only when the catalogue entry has `review_kind`. Cards with a kind of their own keep it.
- History lists kind events as "Made a project card", "Made a knowledge card" and "Follows the Project's kind" with their time.
- Card lists and the card's details show project cards as such (for example a "Project" badge), and the mark of §6.3.

## 6. How project cards behave

Knowledge cards behave exactly as before this capability. The rules below apply only while a card's kind is `project`; swapping back restores the knowledge schedule, because no event is rewritten.

### 6.1 Answers

An answer is a review event of the card with a `rating` other than `"delete"` and no `queue_action`, that no retraction of the same card (an event with `retracted_event_id` equal to its `event_id`) takes back.
The card's latest answer is the latest one by `reviewed_at` (as an instant), then `event_id`.

### 6.2 At most 7 days between reviews

A project card's due time is the earlier of the time the knowledge schedule gives (the client's usual fold, its "scheduled due") and its latest answer's `reviewed_at` plus 7 days.
A project card that has never been answered is due as a new card is today.
Skips (`skip_until`) still hide a card until their time, whatever its kind.
Interval texts after an answer ("Due 24 Nov · in 7 days") show the capped time.

### 6.3 Passage stamp and "changed since you last saw it"

Every answer a client writes, on a card of either kind, carries the passage it was given on:

```json
"passage": {"segments": [{"segment_id": "0", "content_sha256": "3775b1627bce…"}]}
```

The segments are the card's `primary.source_anchor.segments` (version 2) at the moment of the answer, in their order, each reduced to `segment_id` and `content_sha256`.
The stamp is left out when the card has no version-2 segments or any segment lacks a string `segment_id` or `content_sha256`.

A project card is **changed** when its latest answer has `passage.segments` and the card's current segments, taken the same way, differ from them as a map from `segment_id` to `content_sha256` (a segment added, removed or with another hash).
Without a stamp on the latest answer, or without current segments, it is not changed: older answers and cards on imported PDFs are never marked.
An edit of the passage and an accepted proposal that changes it both change `content_sha256` once the card's record follows the source, so both mark the card.

A changed card is due at once: its due time (§6.2) becomes the earlier of that time and the moment of the change, which is the card record's `updated_at` when it is later than the latest answer, else the latest answer's `reviewed_at`.
It is shown with the line **Changed since you last saw it** in review and in card lists.
Answering it records the current passage, so the mark goes and the 7-day rule applies again.

### 6.4 Retiring the project cards of a Project

The Project's menu has **Retire project cards…**, available when the Project holds at least one active (not retired, not deleted) project card.
Its confirmation names the count, says that each card keeps its history and can be restored to review, and that knowledge cards are not touched.
It writes one `retire` event (`review-card-retire.md` §2) for every such card, all with the same `reviewed_at`.
Restore to review brings a card back one at a time, as today.

## 7. Two review entries

Library-wide review gets two entries, each with its own due count, counted as today's due count (active, published, not skipped, due now):

- **Study · N due**: knowledge cards.
- **Projects · M due**: project cards. It can be narrowed to one Project: the choice lists the Projects that hold at least one active project card, each with its own due count, and "All Projects".

A session started from one entry offers only that entry's cards.
Sessions of one document or one card (a reader's Cards list, Review now) keep every card of their scope, whatever its kind.

## 8. Agent tools

- `create_review_card` takes an optional `kind` (`"knowledge"` or `"project"`). Given, the new card's record has `review_kind` (§2.2). Left out, the record has none and the card follows its Project, so the default is the Project's kind. The answer says which kind the card has.
- Card search and listing results (`thoughtsctl search-review-cards` and listings, the desktop MCP tools, the connector's `review_cards` and `get_review_card`) report `review_kind`, `review_kind_source` (`card`, `project` or `default`) and, for project cards, `passage_changed` (§6.3); a tool that reports a due time reports the capped one (§6.2). They may filter by kind.
- The knowledge profile (`thoughtsctl knowledge-profile`, the connector's `knowledge_profile`) classifies knowledge cards only. The cards that match the request are found and ranked as before (including the weak-match cut), then split by kind: `matched`, `counts`, `cards`, the summary's class lines, signals and calibration use the knowledge cards only (`limit` applies to them). When any project card matches, the result has `project_cards`: `{"count": N, "cards": [{"card_id", "question", "document", "project"}…]}` (the first 20 in rank order; `project` only when the card has one), and the summary gets one line: `Project cards on this topic (work in progress, not what the user knows): <questions>` (at most 10 short questions, then `(+K more)`), after the topic's other lines; when no knowledge card matches, the summary's first line says `No knowledge cards match "<topic>": …` instead of `No review cards match …`. Without matching project cards the result is exactly as before. The learner overview leaves project cards out.
- The Mental Model inputs (`tools/mental-model/build.py`) list project cards apart, under a heading that says they describe work in progress, not what the user knows.

## 9. Server

With `review_card_kinds_v1` the server refuses an operation batch (HTTP 409, nothing stored) that contains:

- a review event with `queue_action: "set_review_kind"` whose `schema_version` is not 3, whose `card_id` is not a UUID, whose `reviewed_at` is not an ISO 8601 time with a zone, that carries `rating`, `retracted_event_id` or `interval_action`, or whose `review_kind` is not `knowledge`, `project` or `follow_project`;
- a `review_card` record with a `review_kind` other than `knowledge` or `project`.

It does not check that the card exists, and events may arrive in any order (§3).
It stores and serves answers' `passage` stamps unchanged.
The server keeps no table of kinds and schedules nothing by kind; the fold for agents is `thoughts_cli.review_kinds`, a copy of `thoughts_server.review_kinds`.

## 10. Older clients and servers

An older client ignores kind events (it never reads a queue action as an answer or a skip), the record's `review_kind` and the Project's `review_kind`: every card is a knowledge card there, with today's schedule, in its one review entry.
Nothing is lost: it keeps the events and fields and uploads nothing in their place.
An older server stores kind events, card `review_kind` and answer `passage` stamps unchanged (review events and card records are free-form there), and refuses the `set_review_kind` Project action as an unsupported action; clients offer the Project choice only when the catalogue has `review_kind`.
