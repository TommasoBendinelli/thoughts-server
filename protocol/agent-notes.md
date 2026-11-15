<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Agent notes to the phone

Status: design for branch `feature/agent-notes` (server, desktop `tools/mcp`, Android), 2026-10-15.

While the user studies a review card with an AI agent on the Mac, the agent can send a
visual note (a PNG, or HTML / Markdown with math rendered to a PNG on the Mac). The note
opens on the phone in Thoughts; the user draws on it (pen, highlighter, colours, eraser,
undo) and taps Send. The annotated PNG and the stroke data come back to the agent, both
through MCP tools and as files in a folder on the Mac. Notes stay attached to their
document and review card and can be found again from the phone's Library, tab menu and
review card menu, and from the Mac with `list_notes`.

## Where each piece lives

| Piece | Repository / path |
| --- | --- |
| HTTP API, storage, long-poll | thoughts-server `src/thoughts_server/agent_notes.py`, models in `models.py`, migration `alembic/versions/0022_agent_notes.py`, tests `tests/test_agent_notes.py` |
| MCP tools, HTML/Markdown rendering, reply folder | thoughts-desktop `tools/mcp/agent_notes.py` (stdlib only) + tools in `tools/mcp/server.py`, tests `tools/mcp/test_agent_notes.py` |
| Phone: live channel, notification, annotation, send, Library lists | thoughts-android `data/agentnotes/*`, `ui/agentnotes/*` |

The desktop app (C++) is not changed: the MCP adapter talks to the Thoughts server
directly with the Mac's existing device credential, so nothing needs the running app or
a desktop rebuild/restart.

## Auth reuse

Every endpoint uses the existing bearer device token (`current_device`), exactly like
`/v1/speech`:

* Mac: `~/Library/Application Support/Thoughts/server.json` `server_url`, token from the
  login Keychain item the app itself reads (`/usr/bin/security find-generic-password -s
  thoughts-server -a thoughts-mac -w`; created by `/usr/bin/security`, so no prompt).
  `THOUGHTS_SERVER_URL` / `THOUGHTS_TOKEN` override both (tests).
* Phone: `ThoughtsServerSecretStore.config()` (base URL + device token), like
  `CloudSpeechClient`.

The sender of a note is the device that created it (`sender_device_id`); the phone's inbox
returns notes sent by any *other* device. Replies are listed to the device that sent the
note.

## Why a side channel, not the Sync/outbox model

Library Sync is manual and heavy (sources, builds, PDFs, review entities with device
sequences). Notes are small, append-only, need second-level latency and never feed a PDF
build, so they do not go through `/v1/operations` or device sequences. They follow the
same principles at a smaller scale:

* Content-addressed bytes: images and stroke JSON go into the existing `ObjectStore`
  (`objects` table), so backups and integrity checks cover them.
* Client-chosen UUIDs make every write idempotent: `note_id` for a note, `reply_id` for a
  reply. Retrying the same request returns the stored record; the same id with different
  content is `409 agent_note_conflict`.
* The phone keeps a small local outbox: a reply is written to disk first and sent until the
  server confirms it (retried by the live loop and the periodic worker). Like the
  local-first Library, nothing waits for a Sync.
* Monotonic integer `sequence` columns are the cursors of the inbox and the reply feed.

## Data model (migration 0022_agent_notes)

`agent_notes`

| column | type | notes |
| --- | --- | --- |
| sequence | integer PK autoincrement | inbox cursor |
| id | string(36) unique | client UUID (`note_id`) |
| created_at | timestamptz | server time |
| sender_device_id | string(128) | from the token |
| title | string(200) | required, 1..200 chars after trimming |
| message | text null | optional, <= 4000 chars, shown above the image |
| media_type | string(32) | `image/png` or `image/jpeg` |
| image_sha256 | FK objects.sha256 | |
| image_width, image_height, image_bytes | integer | validated from the header |
| document_id | string(128) null | Library document ID |
| document_filename | string(512) null | Library PDF filename, informational |
| review_card_id | string(128) null | card UUID |
| review_question | string(500) null | snapshot of the card's question when the card entity is known |
| origin | JSON null | e.g. `{"agent": "claude", "conversation_id": "<uuid>"}` |
| content_sha256 | string(64) | hash of the request's semantic content, for idempotency |
| delivered_at, delivered_device_id | timestamptz null, string(128) null | first device that stored it |
| opened_at | timestamptz null | first time it was shown |
| deleted_at | timestamptz null | soft delete; hidden from lists and inbox |

When `review_card_id` is given without `document_id`, the server fills `document_id` from
the card entity (`entities` row `("review_card", "cards", id)`, `payload.primary.document_id`)
and `review_question` from `payload.question` when present.

`agent_note_replies`

| column | type | notes |
| --- | --- | --- |
| sequence | integer PK autoincrement | reply-feed cursor |
| id | string(36) unique | client UUID (`reply_id`) |
| note_id | FK agent_notes.id | |
| device_id | string(128) | the replying device |
| created_at | timestamptz | |
| media_type, image_sha256, image_width, image_height, image_bytes | | annotated image |
| strokes_sha256 | FK objects.sha256 null | stroke JSON (below) |
| stroke_count | integer | |
| message | text null | optional comment, <= 4000 chars |
| sources | string(32) null | migration 0034: what the answering agent may use ("Reply sources"); null when not said |
| content_sha256 | string(64) | idempotency |

`agent_note_listeners`: `device_id` PK, `last_seen_at`, `context` JSON
(`{"review_card_id": ..., "document_id": ...}` when the phone reports one). Updated on every
inbox request, so `POST /v1/agent-notes` can say whether a phone is listening live.

`agent_note_reply_listeners` (migration 0026): PK (`device_id`, `conversation_id`, `note_id`;
the last two `""` when not given), `last_seen_at`. Updated on every reply-feed request with
`listen=true`; rows of a device unseen for 30 days are deleted when it announces again. See
"Who hears a reply".

`agent_note_wakers` (migration 0032): PK (`device_id`, `machine`), `registered_at`, `last_seen_at`, `info`
JSON (what the waker says about itself, and `online_since`), `sessions` JSON (the sessions it can wake). See
"Wakers".

`agent_notes.answer_by` and `agent_note_handling.answer_by` (migration 0031, both JSON null): the
agent the user chose to answer the replies to a note, `{"role", "device_id", "conversation_id",
"owner_id", "label"}`, and that choice as it was when a reply arrived. Null: the automatic choice.
See "The user's choice".

## HTTP API (all JSON unless stated; `Authorization: Bearer <device token>`)

Errors are `{"detail": "...", "error_code": "..."}` with codes `invalid_agent_note` (400),
`agent_note_too_large` (413), `unsupported_image` (415), `agent_note_not_found` (404),
`agent_note_conflict` (409), and `invalid_reply_sources` (422, endpoint 7 only). A missing
or wrong token is the usual 401 and a malformed
query parameter FastAPI's 422. Health advertises the capability `agent_notes_v1`, and
`agent_note_listening_v1` for "Who hears a reply" (endpoint 11, `listen`/`conversation_id` on
endpoint 8, `reply_listening` and `listening` in the answers of endpoints 1 and 7), and
`agent_note_reply_photos_v1` for "Reply photos" (`photo` on endpoint 7 and in reply records),
`agent_note_reply_sources_v1` for "Reply sources" (`sources` on endpoint 7 and in reply records),
`agent_note_links_v1` for "Links" (`links` on endpoint 1 and in note records), and
`agent_note_responder_v1` for "One responder" (`responder` on endpoint 8, endpoint 12, `responder`
in listener records, `sender_device_id` in the feed's `note`), `agent_note_claims_v1` for "Who
answers" (endpoints 13 and 14), `agent_note_owners_v1` for "Topic owners" (endpoints 15 to 17,
owner scopes in conversation feeds and `conversation_id` on endpoint 12, `role`/`owner_id`/`label`
in listener records), `agent_note_rules_v1` for "Answering rules" (endpoint 18), and
`agent_note_answer_by_v1` for "The user's choice" (endpoint 19, `answer_by` and `candidates` in
endpoint 11, `answer_by` in handling records), and `agent_note_wakers_v1` for "Wakers" (endpoints 20 to 23,
`wake` in listening answers, `wake_target` and woken entries in handling records). A server that can push
to the phones also lists `push_fcm_v1`: each new note then pings the other devices' phones
(protocol/push.md).

Image limits: PNG or JPEG (signature checked), decoded body <= 20 MiB, each side
<= 8192 px, width x height <= 40 megapixels. Request bodies above 32 MiB are refused with 413
before parsing.

### Note record (returned everywhere a note appears)

```json
{
  "note_id": "uuid", "sequence": 12, "created_at": "2026-10-15T12:00:00Z",
  "sender_device_id": "thoughts-mac", "title": "...", "message": "..." ,
  "media_type": "image/png", "image_width": 1200, "image_height": 1800, "image_bytes": 123456,
  "image_sha256": "...", "image_url": "/v1/agent-notes/<id>/image",
  "document_id": "...", "document_filename": "...", "review_card_id": "...", "review_question": "...",
  "origin": {...}, "delivered_at": null, "delivered_device_id": null, "opened_at": null,
  "deleted": false,
  "replies": [ <reply record>, ... ]          // oldest first
}
```

### Reply record

```json
{
  "reply_id": "uuid", "sequence": 3, "note_id": "uuid", "device_id": "phone",
  "created_at": "...", "media_type": "image/png", "image_width": 1200, "image_height": 1800,
  "image_bytes": 234567, "image_sha256": "...", "image_url": "/v1/agent-notes/replies/<id>/image",
  "strokes_url": "/v1/agent-notes/replies/<id>/strokes" (or null), "stroke_count": 7, "message": "...",
  "photo": null | {"url": "/v1/agent-notes/replies/<id>/photo", "media_type": "image/jpeg",
            "width": 3264, "height": 2448, "bytes": 1234567, "sha256": "...", "capture": "IMG_..."},
  "sources": null | "none" | "linked_pdf" | "library" | "library_and_mental_model"
}
```

### Endpoints

1. `POST /v1/agent-notes` — create.
   Body: `{"note_id", "title", "message"?, "image": {"media_type", "data_base64"},
   "document_id"?, "document_filename"?, "review_card_id"?, "origin"?}`.
   `201` with `{"note": <note>, "delivery": {"live": bool, "listeners": [{"device_id",
   "last_seen_at", "context"}]}}`; `live` is true when another device made an inbox request
   in the last 45 s; `listeners` are the other devices that asked their inbox in the last
   24 h, most recent first. Same `note_id` and same content: `200` with the stored note
   and `delivery`. `review_question` is filled whenever the card entity is known. The answer
   also has `"reply_listening": {"state", "listeners"}` (endpoint 11) for the new note.
2. `GET /v1/agent-notes?document_id=&review_card_id=&sent_by_me=&limit=` — newest first,
   `limit` 1..200 (default 50, clamped), deleted notes excluded; `sent_by_me=true` only the
   caller's notes, `false` only other devices'. `{"notes": [...]}`.
3. `GET /v1/agent-notes/inbox?after=&wait=&review_card_id=&document_id=` — the phone's
   long-poll. Returns notes with `sequence > after` not sent by the caller and not deleted,
   oldest first, at most 20, and `cursor` (the highest sequence returned, or `after`).
   Without `after`, returns the notes not yet delivered from the last 24 h and `cursor` =
   the current highest sequence (when more than 20 are undelivered: the last one returned,
   so the next request continues with them). `review_card_id` / `document_id` only set the
   listener `context`; they do not filter. `wait` 0..30 s: when nothing is new, the request waits up
   to `wait` seconds and answers as soon as a note is created (in-process asyncio condition,
   re-checked from the database every second). Every call records the listener row.
   `{"notes": [...], "cursor": 12, "server_time": "..."}`.
4. `GET /v1/agent-notes/{note_id}` — one note with its replies (also a deleted one, with
   `"deleted": true`).
5. `GET /v1/agent-notes/{note_id}/image` — the bytes, `Content-Type` = media type,
   `ETag` = `"<sha256>"` (quoted; `If-None-Match` answers 304),
   `Cache-Control: private, max-age=31536000, immutable`.
6. `POST /v1/agent-notes/{note_id}/receipt` — `{"state": "delivered" | "opened"}`; the
   first time stamp wins; `opened` also sets `delivered_*` if unset. Returns the note.
7. `POST /v1/agent-notes/{note_id}/replies` — `{"reply_id", "image": {"media_type",
   "data_base64"}, "strokes"?: <stroke document>, "message"?, "photo"?: {"media_type",
   "data_base64", "capture"?}, "sources"?: "none" | "linked_pdf" | "library" |
   "library_and_mental_model" | null}`. `201` with the reply,
   `200` on an identical retry. A reply to a deleted note is still accepted. Any other `sources`
   (another string, or not a string) is `422` `invalid_reply_sources` ("Reply sources").
   The answer is the
   reply record plus `"listening": {"state", "listeners"}` (endpoint 11) as of now, so the
   phone can say right after Send whether an agent hears it; stored records do not carry it.
8. `GET /v1/agent-notes/replies?after=&wait=&note_id=&conversation_id=&listen=` — replies to
   notes the caller sent (or, with `note_id`, to that note when the caller sent it; with
   `conversation_id`, to notes whose `origin.conversation_id` is that value), `sequence >
   after`, oldest first, at most 50, each with `"note": {note_id, title, document_id,
   review_card_id, sender_device_id}`. `wait` 0..60 s long-poll as for the inbox. `{"replies": [...], "cursor": n}`.
   `listen=true` records the caller as a reply listener for (`conversation_id`, `note_id`).
   `responder=true`: replies to every device's notes, not only the caller's ("One responder");
   with `listen=true` the caller listens for all of them. It cannot be combined with `note_id` or
   a non-blank `conversation_id` (400 `invalid_agent_note`).
9. `GET /v1/agent-notes/replies/{reply_id}/image`, `GET /v1/agent-notes/replies/{reply_id}/strokes`,
   `GET /v1/agent-notes/replies/{reply_id}/photo` (404 when the reply has none).
10. `DELETE /v1/agent-notes/{note_id}` — soft delete; `204`. Any device may delete.
11. `GET /v1/agent-notes/{note_id}/listening` — `{"note_id", "state": "listening" |
    "not_listening" | "unknown", "listeners": [{"device_id", "conversation_id", "note_id",
    "last_seen_at", "responder"}], "answer_by": null | <agent>, "candidates": [<agent>],
    "checked_at"}`; `listeners` are the live ones that hear this note. `answer_by` is the agent the
    user chose to answer this note's replies, or null when the choice is automatic; with a choice,
    `state` is `listening` while that agent is live and `not_listening` otherwise, and `listeners`
    holds its listener record or nothing. `candidates` are the agents that could answer now ("The
    user's choice"). An agent is `{"role": "sender" | "owner" | "responder", "device_id",
    "conversation_id", "owner_id", "label", "live"}`.
12. `GET /v1/agent-notes/pages?after=&wait=` — the pages the user sent to the agents: notes whose
    `origin.kind` is `phone_page` (the phone's study mode, the Mac's Send Page to Agent), from any
    device, not deleted, `sequence > after`, oldest first, at most 20. `wait` 0..60 s long-poll as
    for the inbox. Without `after`: no notes and `cursor` = the current highest note sequence, where
    a new reader starts. `{"notes": [...], "cursor": n}`. It records nothing (no inbox listener
    row, so senders' `delivery.live` is unchanged). With `conversation_id`, only the pages of the
    topics that conversation of the caller's device owns ("Topic owners").
13. `GET /v1/agent-notes/handling/{item_id}` — a reply's (`item_id` = reply id) or page's
    (`item_id` = its note id) handling: `{"item_id", "kind": "reply" | "page", "note_id",
    "arrived_at", "covered_by": [<listener record> + "live"], "claim": null | {"device_id",
    "conversation_id", "role", "claimed_at", "answer_note_id"}, "inferred_answer_note_id"}`. A
    reply that arrived while the user had chosen who answers its note also has `"answer_by":
    <agent>` (that choice, `live` as of now).
14. `POST /v1/agent-notes/handling/{item_id}/claim` — `{"conversation_id"?, "responder"?: bool,
    "only_if_covering"?: bool}`; `201` claimed now, `200` already the caller's (same device and
    conversation), `409` with the handling record and `error_code` `agent_note_claimed` (another
    agent has it), `agent_note_covered` (responder only: `retry_after` seconds) or
    `agent_note_not_covering`. See "Who answers"; for a reply under the user's choice, "The user's
    choice".
15. `POST /v1/agent-notes/owners` — `{"conversation_id", "label"?, "document_ids"?,
    "review_card_ids"?, "title_tags"?, "captures"?: bool}` (at least one of the last four);
    `201` (`200` when that conversation already owns that scope) with the owner record
    `{"owner_id", "device_id", "conversation_id", "label", "document_ids", "review_card_ids",
    "title_tags", "captures", "last_seen_at", "live"}`.
16. `GET /v1/agent-notes/owners` — `{"owners": [...], "checked_at"}`: every owner seen in the last
    30 days, most recent first, with `live`.
17. `DELETE /v1/agent-notes/owners/{owner_id}` (any device; `404` when unknown) and
    `DELETE /v1/agent-notes/owners?conversation_id=` (every ownership of the caller's
    conversation) — `204`.
18. `GET /v1/agent-notes/rules` — `{"text", "sha256", "source_path", "revision", "updated_at",
    "others"}`, or `404` `agent_note_not_found` when the Library has no rules note.
19. `PUT /v1/agent-notes/{note_id}/answer-by` — `{"agent": {"role", "device_id", "conversation_id",
    "owner_id"}}` (other fields of an agent are ignored), or `{"agent": null}` for the automatic
    choice. Any device may call it; it is idempotent. `200` with the same body as endpoint 11.
    `404` for an unknown or deleted note, `422` `invalid_agent_note` for a malformed body, `409`
    `agent_note_answerer_unknown` when no listener row, live or not, is that agent.
20. `PUT /v1/agent-notes/wakers/{machine}` — a reply waker's heartbeat (`machine`: 1 to 64 letters, digits,
    `.`, `_`, `-`). With `"sessions": [{"session_id", "title"?, "folder"?, "state": "idle" | "busy" | "closed",
    "kind"?, "last_active_at"?, "wakeable"?: bool}]` (at most 500) it is a full registration and replaces the
    stored one, with `"fallback"?: {"session_id", "title"}`, `"fallback_after_seconds"?` (60 s to 7 days,
    default 8 hours), `"heartbeat_seconds"?`, `"version"?`, `"claude_version"?`; without `sessions` it only
    beats and keeps what was registered. `201` on the first beat, else `200`, with the waker record
    `{"device_id", "machine", "live", "registered_at", "last_seen_at", "fallback", "fallback_after_seconds",
    "heartbeat_seconds", "version", "claude_version", "sessions"}`.
21. `GET /v1/agent-notes/wakers` — `{"wakers": [...], "checked_at"}`: every waker seen in the last 30 days, live
    ones first.
22. `DELETE /v1/agent-notes/wakers/{machine}` — the caller's waker on that machine stops for good; `204`, `404`
    when unknown.
23. `POST /v1/agent-notes/handling/{item_id}/handover` — `{"machine", "reason"?}`: the caller's waker on that
    machine could not wake the session it keeps the item for. Its woken entries get `handed_over_at` and
    `handover_reason`; a claim that device made for one of those sessions is given up (not an `answered` one).
    `200` with the handling record plus `"handed_over": n, "released": bool`.

### Who hears a reply

A reply is stored for the note's sender, but an agent acts on it only while it waits for it.
Agents that will act on replies say so: they poll endpoint 8 with `listen=true`, a
`conversation_id` when they are one conversation (they then get only that conversation's
replies), and `note_id` when they wait for one note. Polls without `listen` (a folder sync)
record nothing. A note's state:

* `listening`: a listener of the note's sender device was seen in the last 90 s whose
  `note_id` is empty or the note and whose `conversation_id` is empty or the note's
  `origin.conversation_id` (exactly the listeners whose feed holds the note's replies).
* `listening` also when a responder (below) was seen in the last 90 s, whatever device sent the note.
* `not_listening`: none is, but agents of that device, or a responder, announced listening in the
  last 30 days.
* `unknown`: the sender's agents never announce it (older clients, the connector today) and no
  responder does; clients show nothing.

The phone shows `not_listening` on the note before the user replies, and the state in the
reply's answer after Send.

### One responder

One agent may answer for every device: it polls endpoint 8 with `responder=true` (and
`listen=true`), so its feed holds the replies to every note, whichever device sent it, and every
note is `listening` while it re-polls within 90 s. Its listener row is one per device, stored with
`note_id` `*` (real note IDs are UUIDs) and empty `conversation_id`; listener records show it as
`"responder": true` with `note_id` null. It reads the user's pages with endpoint 12. The other
agents keep their own feeds and listeners; a sender's own listener and a responder may both hear a
note, so agents that leave replies to the responder should stop listening themselves.

### Who answers

Any agent may answer the user's replies and pages, but one answer per item, and the agent that
is responsible goes first:

* **Coverage at arrival.** When a reply is stored, the server records which live listeners
  covered its note at that moment (`covered_by`): agents of the sender device whose feed holds
  it (its conversation, or device-wide), and topic owners whose scope holds the note. Responders
  are not listed. A page the user sends records the topic owners that cover it.
* **Claims.** An agent claims an item before it answers it (endpoint 14); the first claim wins
  (a conditional update), so two agents never both answer. The claimant is the device plus its
  `conversation_id`. With `only_if_covering`, the claim succeeds only for an agent that covered
  the item at arrival, covers it now, or (replies) sent the note from that conversation; tools
  claim this way when they show a reply or page to their agent.
* **The responder waits.** A claim with `responder: true` waits (`409 agent_note_covered`,
  `retry_after`) while an agent in `covered_by` still listens, for up to 10 minutes after
  arrival. It does not wait for replies to its own notes (same device and conversation as the
  note's `origin.conversation_id`), nor for coverers that stopped listening.
* **Answers seen as claims.** Agents with tools from before claims never claim. When an item has
  no claim, a note sent after it arrived by one of its coverers counts as that coverer's claim
  (`role: answered`, `answer_note_id`): from the same conversation when the coverer is one, else
  from the same device on the same document or card. The user's pages and notes whose
  `origin.responder` is true never count.

A reply or page from before claims gets its handling row on first use, with `covered_by` empty.

### The user's choice

Without a choice, "Who answers" decides. The user may instead choose, per note, which agent answers
its replies (endpoint 19, usually from the phone). The choice is one of three kinds of agent: a
`sender` (a listening agent of a device, with its conversation, or device-wide when
`conversation_id` is null), an `owner` (a topic owner, by `owner_id`), or the `responder` of a
device. Endpoint 11 lists the `candidates`: the live ones only, each once, the sender listeners
that cover the note first, then every live topic owner (those whose scope holds the note first),
then the responder. Any agent with a listener row may be chosen, also one whose own notes and
topics do not include the note.

* **At arrival.** A reply keeps the choice its note had when it arrived (`answer_by` in its handling
  row); `covered_by` then lists only the chosen agent's listener (live or not; none when it has no
  listener row any more), and the `listening` of endpoint 7's answer describes that agent. A later
  change of the choice, or the automatic choice again, is for the replies that arrive afterwards:
  reply feeds are cursors, so an agent chosen later may already be past an earlier reply and would
  never see it, while its being live would keep every other agent away.
* **Delivery.** The chosen agent's reply feed (endpoint 8, its `conversation_id`, or none for a
  device-wide sender) holds those replies even when its own notes and topics do not. A responder's
  feed holds every reply anyway. The other agents' feeds are unchanged.
* **Claims.** The chosen agent claims at once; a chosen responder does not wait for the agents in
  `covered_by`. Every other claim is refused with `409` and `detail` "the user chose another agent
  to answer this note": `agent_note_not_covering` for a sender or owner (always), and
  `agent_note_covered` with `retry_after` for a responder while the chosen agent is live or until 10
  minutes after arrival. After that a responder (only) may claim, so no reply is lost when the
  chosen agent is gone. These are the codes on which tools already leave an item to another agent.
* **Answers seen as claims** count for the chosen agent only; a chosen responder always claims.
* **Lifetime.** Deleting the note removes its choice. Releasing a topic, or a listener row that
  disappears, leaves the choice in place but not live: `answer_by.live` is false, the note is
  `not_listening`, and a responder takes its replies as above.

Pages the user sends are not replies: they are answered as in "Who answers", whatever is chosen.

### Wakers

A reply waker is a program on one machine (the server, the Mac) that wakes that machine's Claude Code sessions
when a reply or a page is theirs, so a session needs no running watcher to answer. It beats every 30 s or so
(endpoint 20) with the sessions it can wake: running ones (it messages them) and closed ones it may resume. A
waker is live while it beat within 2 minutes (or three of its own `heartbeat_seconds`). One waker may name a
`fallback` session: the one that answers what has no other home (for example a standing session on the
server). It also polls
the reply feed as the responder, as the one responder did.

* **Coverage at arrival.** Besides the live listeners, `covered_by` lists the sessions a waker of the right
  device can wake: the note's own session (`origin.conversation_id`, registered by a waker of the note's sender
  device), then the sessions of topic owners whose scope holds the note (their listeners need not be live). Such an
  entry has `"woken": true`, the waker's `machine` and the session's `title`; when the session also listens, its
  listener entry is marked so. With the user's choice, only the chosen agent's entry, marked when a waker wakes
  it. Entries are ordered the note's own session first, then topic owners.
* **The wake target.** A handling record has `wake_target`: the first entry that still covers the item when a
  waker wakes it, `{"state": "will_be_woken" | "waker_offline", "role", "device_id", "machine",
  "conversation_id", "title", "session_state", "owner_id", "label", "waker_live", "fallback_at"}`. An entry that
  only listens (no waker wakes it) covers it by itself, so there is no `wake_target`. An entry whose waker stays
  offline past its fallback time, or handed the item over, is passed by. With nobody left, or the responder
  chosen, it is the fallback session (`role: responder`), when a waker names one. A waker acts only on a
  `wake_target` of its own machine and device: it claims the item for that session (endpoint 14 with its
  `conversation_id`), then wakes it; the session's own claim later answers `200` (already its own).
* **The responder waits.** A responder's claim waits (`agent_note_covered`, `retry_after`) for a woken entry for
  10 minutes after arrival while its waker is live (10 minutes after the waker came back online, when it was
  offline), and while the waker is offline, until `fallback_after_seconds` (8 hours) after arrival, so the
  fallback answers a sleeping machine's replies only then. Under the user's choice, a chosen session a live
  waker wakes keeps the reply as long as its waker is live, and for the fallback time while it is offline.
* **Hand-over.** A waker that cannot wake the session (it is gone, not resumable, or the wake failed) calls
  endpoint 23; the responder then claims at once.
* **What the phone shows.** Listening answers (endpoints 1, 7 and 11) carry `wake`, the `wake_target` a reply
  arriving now would get, absent when there is none. `state` and `listeners` keep their meaning (only listeners
  count), so older clients read the same answer as before. A client shows "<title> will be woken (on <machine>)"
  for `will_be_woken` and, for `waker_offline`, that the machine is offline and the fallback answers at
  `fallback_at`. Every field is absent from every answer until a waker registers.

### Topic owners

The user may make one conversation responsible for a topic: a project's documents, document IDs,
review card IDs, title tags (a note is in the topic when its title contains the tag, in any case)
and, optionally, the glasses captures (`captures`, read by the agent that watches the capture
folders). The conversation claims it with endpoint 15. The ownership is a scoped reply listener
(a row of `agent_note_reply_listeners` with `note_id` `topic:<owner_id>` and the scope): it is
live while the conversation keeps polling endpoint 8 with `listen=true` and its `conversation_id`
(each such poll refreshes every ownership of that conversation), and ends with endpoint 17.
While it is live, every note in its scope is `listening` with a listener of `role: owner` and the
topic's `label`, replies and pages in its scope record it in `covered_by`, the conversation's feed
(endpoint 8 with its `conversation_id`) also holds the replies to every device's notes in the
scope, and endpoint 12 with its `conversation_id` lists the pages in the scope.

### Answering rules

The user's rules for every agent that answers a reply or page live in one Library note named
`agent-note-rules.md` (in any Project or docs folder). Endpoint 18 serves its current text as the
user accepted it: pending agent proposals are rejected and Thoughts marker comments (card markers)
removed. Tools hand this text to their agent with each reply or page, so agents on every device
follow the same rules, and the user changes them by editing that note. With several such notes the
most recently updated one is served and `others` names the rest. The server ships no default text
and enforces one rule itself: when the user chose who answers a note, only that agent answers it.

### Reply photos

A reply may carry one `photo` besides its annotated `image`: the phone's "Send with picture" takes
a picture with the user's glasses and sends it with the typed reply, so the instruction and the
picture reach the same agent (the note's sender) instead of the picture going to whichever agent
watches the glasses. Same limits as `image` (PNG or JPEG, 20 MiB, 8192 px a side, 40 MP); the
whole request stays under 32 MiB. `capture` (<= 128 characters) names where it came from, e.g. the
glasses' capture `IMG_<time>_<suffix>`. The glasses also deliver every capture to their usual
archive, so a watcher that answers glasses captures must leave a capture alone when a reply names
it in `photo.capture`: that reply's agent answers it. The photo is part of the reply's identity: a
retry with another photo (or without it) under the same `reply_id` is a conflict.

### Reply sources

With each reply the user chooses (on the Mac or the phone) what the agent that answers it may use to
ground its answer, sent as `sources` (migration 0034):

* `none`: no Thoughts documents at all; the agent answers from general knowledge and the note and
  reply only.
* `linked_pdf`: only the PDF the note is linked to (its `document_id` / `document_filename`).
* `library`: any Library document, but not the user's Mental Model.
* `library_and_mental_model`: Library documents and the Mental Model (what agents did before the choice).
* `null` (or absent): the sender did not say, so the agent uses everything, as before.

Any other value is refused with `422` `invalid_reply_sources` and nothing is stored. Every reply record
carries `sources` (`null` for replies stored before the field, or sent without it). The server only stores
and returns the choice; the agent that answers follows it, and the tools that hand a reply to their agent
pass it on. `sources` is part of the reply's identity, like `message`: a retry with other sources (or
without them) under the same `reply_id` is a conflict; a reply without `sources` hashes as before. A server
without `agent_note_reply_sources_v1` ignores the field, so senders check the capability (or the `sources`
of the answer) before showing the choice as sent.

### Links

A note may carry `links` (migration 0029): rectangles of its image that open a source when the
user taps them, so a reference drawn into the picture ("p. 53–54", a paper's name) leads to it.
Each entry is

```json
{"rect": [x, y, width, height], "href": "thoughts-pdf-link:<Library filename>.pdf?page=52", "label": "p. 53–54"}
```

* `rect`: fractions of the image's width and height (0 to 1), inside the image.
* `href`: `https://…`, `http://…`, or `thoughts-pdf-link:<Library filename>.pdf?page=<0-based page>`
  (optionally `&x=&y=` in PDF points), the form PDF links to other Library PDFs take in the reader.
* `label` (optional, <= 200 characters): the linked text, for lists and accessibility.

At most 64 links, href <= 2048 characters; anything else is `invalid_agent_note`. Note records
always carry `links` (`[]` when there are none). Links are part of the note's identity: a retry
with other links under the same `note_id` is a conflict; a note without links hashes as before.
A server without `agent_note_links_v1` ignores the field, so senders check the capability (or
the `links` of the answer) before relying on it.

The Mac's `send_note` fills `links` itself from `<a href>` elements of an HTML or Markdown note,
measured in the same layout as the screenshot; a link to `<Library filename>.pdf#page=N`
(1-based, as printed) becomes `thoughts-pdf-link:…?page=N-1`. The phone opens a tapped link in
pan mode: a Library page in the reader (the same path as a PDF link), a web page in the browser.

## Stroke document (schema 1)

```json
{
  "schema_version": 1,
  "image_width": 1200, "image_height": 1800,          // pixel space of the reply image
  "source_width": 1200, "source_height": 1800,        // the note image as sent
  "strokes": [
    {"tool": "pen" | "highlighter", "color": "#RRGGBB", "opacity": 1.0,
     "width": 6.0, "points": [[x, y], ...]}            // image pixels, 1 decimal
  ]
}
```

Eraser removes whole strokes it touches; the document contains only the strokes still
present. Highlighter strokes are drawn under pen strokes with opacity 0.35 and a width of
about 4x the pen. The server checks only structure and size (<= 2 MiB, <= 5000 strokes,
<= 200 000 points).

## Phone

* **Live channel ("study mode").** One `AgentNoteInbox` loop long-polls
  `/inbox?wait=25` (read timeout 40 s; back-off 2 s → 60 s on errors) while either
  (a) a Thoughts activity is started (foreground), or (b) the *study listener* foreground
  service runs. The service (type `dataSync`, low-importance ongoing notification
  "Listening for agent notes", action Stop) starts when the user taps a review card's
  "Copy for agent" / "Copy for physical studying" or the tab's "Copy for agent", opens an
  agent note, or sends a reply, and stops 30 minutes after the last of these. While a review
  card is shown, the inbox request carries its `review_card_id` and `document_id`.
* **Arrival.** The note's image is downloaded into
  `filesDir/thoughts/agent-notes/<note_id>/`, `delivered` is posted, then: if a Thoughts
  activity is resumed, `AgentNoteActivity` opens immediately (about 1–2 s after
  `send_note`); otherwise a high-importance notification "Agent note: <title>" opens it
  on tap.
* **Not listening.** A periodic WorkManager job (15 min, network required) asks the inbox
  with `wait=0`, stores new notes, notifies, and retries unsent replies.
* **Push.** A server with `push_fcm_v1` sends the phone a content-free FCM ping (`{"kind":
  "agent_notes", "at"}`, nothing about the note) whenever a new note is in its inbox, so the phone can
  ask the inbox at once without the live channel running. The phone registers its FCM token with
  `PUT /v1/push/registration`. See protocol/push.md.
* **Annotation.** One finger draws; two fingers pan and zoom. Tools: pen, highlighter,
  eraser (whole strokes); colours black, red, blue, green, orange; undo and redo. The draft
  is kept on disk until sent, so it survives rotation and process death.
* **Send.** The composite (note image + strokes, same pixel size, or the decoded size
  when a very large image had to be scaled down) is written as PNG with the stroke
  document into the local outbox, then posted; on failure it stays queued and is retried.
  Optional comment via the toolbar.
* **Finding notes again.** "Agent notes" in the review card ⋮ menu (that card), the
  reader tab menu and the Library item menu (that document), and Home's menu (all notes).
  The list merges the server list with the local cache, shows thumbnail, title, time and
  "Answered"/"Not answered"; replies open read-only.

## Mac

MCP tools (adapter `tools/mcp/server.py`, logic in `tools/mcp/agent_notes.py`):

* `send_note(title, image_path | html | html_path | markdown, message?, review_card_id?,
  document_id?, document_filename?, conversation_id?, note_id?)` — renders HTML/Markdown
  with headless Google Chrome (private temporary profile, phone-width page 480 CSS px at
  2.5x, full page height up to 8192 px; Markdown via marked + KaTeX), uploads, writes the
  note into the folder and returns `note_id`, `delivery.live`, the local path and what to
  tell the user when no phone is listening.
* `list_notes(review_card_id?, document_id?, limit?)` — notes with their replies.
* `list_note_replies(note_id?, after?)` — replies to notes this Mac sent; downloads them
  into the folder; returns local paths.
* `get_note_reply(note_id, wait_seconds=0..600)` — the newest reply to a note, waiting for
  one if asked; returns local paths, the stroke summary and the annotated image itself as
  MCP image content (when <= 4 MB) so the agent can look at it directly.

Reply folder: `~/Thoughts/agent-notes/` (outside the stalling `~/Documents`, `~/Desktop`,
`~/Downloads`, and outside the Library root `~/Thoughts/docs`); `THOUGHTS_AGENT_NOTES_DIR`
overrides it.

```
~/Thoughts/agent-notes/
  <YYYYMMDD-HHMMSS>-<slug>-<note8>/
    note.png  note.json  [note.html | note.md]
    reply-1.png  reply-1.strokes.json  reply-1.json     # one set per reply, numbered
  replies.jsonl                                         # one line per reply as it lands
  .state.json                                           # reply-feed cursor
```

Files are written to a temporary name and renamed, so a watcher never sees a partial PNG.
While the MCP server process runs and this Mac sent a note in the last 24 h, a background task long-polls `/v1/agent-notes/replies?wait=25` and writes every
reply as it arrives, so an agent can simply watch the folder (e.g. for `reply-*.png`).
`python3 tools/mcp/agent_notes.py watch` does the same outside MCP. These folder syncs do not
pass `listen`: they wake no agent, so they must not make the phone say an agent is listening.
`thoughts-watch-replies` (a monitor an agent runs in the background) and `get_note_reply`
with `wait_seconds` do pass it ("Who hears a reply").

## Not in scope

Desktop (C++) Library UI for notes, editing a note after sending, text
recognition of handwriting.
