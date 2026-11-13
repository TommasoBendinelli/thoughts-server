<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Review card feedback

During a review, once a card's answer is visible, the user selects a passage of the answer and
marks it **liked** (positive) or **disliked** (negative), optionally with a comment. The
highlight stays attached to that passage of that card. It is drawn again on every later review
of the card, but only while the answer is visible. It stays until the user deletes it. Agents
read it with the card. The user's HCI study uses the same data (participants mark good and bad
sentences), so the polarity, the exact quoted text and the timestamps are part of the contract.

Status: server `review_card_feedback_v1` (branch feature/review-card-feedback). Desktop and
Android implement the client rules in §5–§7.

## 1. Why a separate record

The feedback is not a field of `review_card` or of a `review_event`:

- The server rewrites `review_card` all the time (re-anchoring, linking, moves; real cards have
  revision 70+). If highlights lived in the card payload, they would race those writes, and
  every highlight edit would retrigger card placement logic.
- Review events are immutable answer records ("first write wins") created on Next. A
  highlight is edited and deleted later, often outside the answer that created it.
- A separate record per highlight follows the existing mutable record mechanism
  (`pdf_link_override`, `review_tag_groups`): the same `/v1/operations` outbox, device sequences,
  offline queue, `/v1/changes` cursor and `/v1/projections` bootstrap. No new endpoint or
  channel is needed. Two highlights never conflict with each other.

## 2. Record

- `entity_type`: `"review_card_feedback"`
- `collection`: `"review-feedback"`
- `entity_id`: a random canonical (lowercase) UUIDv4 created by the client, equal to
  `payload.feedback_id`
- Delivered by `/v1/changes` (including tombstones), `/v1/projections` (non-deleted only) and
  the `/v1/document-sync` snapshot of the PDF that owns the card (the card's
  `primary.pdf_stem`).
- Capability: `"review_card_feedback_v1"` in `/v1/health` `capabilities`. Clients keep their
  feedback operations queued, and do not upload them, until the server advertises it. They may
  create and show feedback locally before that.
- The record has no render impact. It is not part of the review snapshot used by PDF builds,
  so creating or editing feedback never triggers a build.

```json
{
  "schema_version": 1,
  "feedback_id": "5b0f6c1e-2a47-4d1b-9c35-7e0a4f2b9d13",
  "card_id": "0072b138-d3fe-416a-83e7-1407058f0e2b",
  "polarity": "negative",
  "comment": "This sign is wrong: the KL term is subtracted.",
  "anchor": {
    "version": 1,
    "quote": {
      "exact": "the KL term keeps the policy close",
      "prefix": "In practice, ",
      "suffix": " to the reference model."
    },
    "pdf": {
      "document_id": "c1e62ecc-2dfb-4259-97cc-e319b1186848",
      "pdf_sha256": "f4c8827551095a63ce58c6b1e2c5946014b7b3f62d0f96a780a2931f601a561e",
      "regions": [
        {"page": 7, "rect": [0.1765, 0.4116, 0.8256, 0.4290]},
        {"page": 7, "rect": [0.1765, 0.4290, 0.3537, 0.4460]}
      ]
    },
    "segment_id": "0",
    "segment_content_sha256": "3775b1627bce1bbc61bdd810a212daab7db3fbfc7932002aacc578a9954d87cd"
  },
  "revision": 1,
  "created_at": "2026-10-20T10:14:03.512Z",
  "updated_at": "2026-10-20T10:14:03.512Z",
  "created_origin_id": "android-pixel",
  "origin_id": "android-pixel",
  "deleted": false
}
```

Fields:

| field | rule |
|---|---|
| `schema_version` | `1` |
| `feedback_id` | canonical lowercase UUIDv4, equals `entity_id` |
| `card_id` | the card's `card_id`, 1–128 characters. The server does not require the card to exist yet, because a card made offline may arrive later. |
| `polarity` | `"positive"` (UI: Like / Liked) or `"negative"` (UI: Dislike / Disliked) |
| `comment` | free text, 0–4000 characters, `""` when there is none. Kept exactly as typed; clients trim only leading and trailing whitespace on save. |
| `anchor` | §3. **Immutable**: fixed by the first write. |
| `revision` | integer ≥ 1. The first write is 1. Every later write is the highest revision the device knows, plus 1. |
| `created_at` | RFC 3339 with timezone (`Z` or an offset), set once. **Immutable.** |
| `updated_at` | RFC 3339 with timezone, the time of this write |
| `created_origin_id` | the `origin_id` of the device that created it. **Immutable.** |
| `origin_id` | the device/origin id of this write, as for other records (`mac-okular`, the phone's id) |
| `deleted` | boolean. A delete is a tombstone that carries the full record. |
| `deleted_at` | RFC 3339, required when `deleted` is true and absent otherwise |

Unknown fields, including those inside `anchor`, are stored and returned unchanged. Clients
must keep them when they rewrite a record.

Server validation (HTTP 422 for the whole batch otherwise):

- `collection` is `review-feedback`, and `feedback_id` equals `entity_id` and is a UUIDv4.
- `polarity` is `positive` or `negative`.
- The timestamps carry a timezone.
- `deleted` and `deleted_at` are consistent.
- `anchor.version` is 1.
- `quote.exact` has 1–4000 characters and is not blank; `prefix` and `suffix` have at most 200
  characters each.
- `view` is `pdf` (the default) or `html`. With `view: "html"` (feedback made in the HTML
  view, `docs/html-edition.md`) `html_edition_sha256` (64 lowercase hex digits, the edition that
  was displayed) is required and `anchor.pdf` is optional (if present, `regions` may be empty);
  with `view: "pdf"` `anchor.pdf` with at least one region is required. Both fields are fixed by
  the first write, like `anchor`.
- `pdf.document_id` is a UUIDv4 and `pdf.pdf_sha256` is 64 lowercase hex digits.
- `regions` has 1–200 entries (feedback made in the PDF view). Each has `page` ≥ 1 and a `rect` of four numbers in [0, 1] with
  left ≤ right and top ≤ bottom.
- `segment_id`, if present, has 1–64 characters, and `segment_content_sha256`, if present, is
  64 hex digits.

## 3. Anchor

The anchor records where the passage was when the feedback was made. It is never rewritten:
clients find the passage again each time they display it (§4).

- `quote.exact`: the selected text as the creating client's text selection gives it, with
  leading and trailing whitespace trimmed. Agents and the study read this as "the passage".
- `quote.prefix` / `quote.suffix`: up to **32** characters of the answer text (§4.1)
  immediately before and after the selection, from the creating client's extraction. Use `""`
  when nothing comes before or after, or when the client cannot compute it.
- `pdf.document_id`, `pdf.pdf_sha256`: the document and exact PDF bytes that were displayed.
- `pdf.regions`: the selection's line rectangles, in the **same convention as the card's
  `primary.regions`**: `page` is 1-based, and `rect` is `[left, top, right, bottom]`
  normalized to the page (top-left origin, 0..1). Use one rectangle per selected line (merge
  the glyph boxes of a line) and list them in reading order.
- `segment_id`, `segment_content_sha256`: for source-linked cards (`primary.source_anchor`
  version 2), the segment that holds the passage and its `content_sha256` at creation.
  Set them when the card has exactly one segment, or when the client knows which segment's
  area contains the selection. Otherwise omit them. They are informational: they tell agents
  and clients whether the answer's source changed since the feedback was made.
- No character offsets are stored. Poppler (desktop) and PDFium (Android) extract different
  text from the same page, so offsets would not mean the same thing on both clients. The quote
  plus its context is portable.

Feedback is allowed on every card type: source-linked cards, PDF-only cards on imported PDFs,
multi-area and multi-page cards, and whole-document cards (whose answer is every page).

## 4. Finding the passage again (clients, at display time)

This runs each time a card's answer becomes visible. The result is never written back.

### 4.1 Answer text

The answer text of a card on the displayed PDF is built from the card's current
`primary.regions`, taken in list order (for a whole-document card: every page, in order). For
each region, take the page characters whose glyph box centre lies inside the region `rect`, in
the platform's reading order. Join the regions with `"\n"`.

### 4.2 Algorithm

1. **Same PDF**: if `anchor.pdf.pdf_sha256` equals the SHA-256 of the displayed PDF, draw
   `anchor.pdf.regions` as they are. State `exact`.
2. **Skeleton match**: otherwise, build `skeleton(s)` one code point at a time:
   - A code point of category Mn (a combining mark) contributes nothing. Its UTF-16 range is
     added to the previous kept character.
   - Every other code point is NFKD-decomposed. The resulting characters of category Zs, Zl,
     Zp, Cc, Cf or Mn are dropped (this covers spaces, NBSP, newlines, tabs, U+00AD soft hyphen
     and zero-width characters). Each kept character records the UTF-16 range of the code point
     it came from.

   This makes the ligature `ﬁ` match `fi`, `é` match `é`, and `𝒫` match `P`. Search for
   `skeleton(quote.exact)` in `skeleton(answer text)`, case-sensitive, at every position
   (overlapping hits count).
3. **Hyphen retry**: if there is no hit, also remove U+002D, U+2010 and U+2011 from both sides
   and search again.
4. **No hit**: the state is `orphaned` (§6.4).
5. **Several hits**: score each hit as
   `commonSuffixLength(skeleton(prefix), skeleton(text before hit)) + commonPrefixLength(skeleton(suffix), skeleton(text after hit))`.
   The highest score wins. On a tie, the first hit in answer-text order wins.
6. **Draw**: the drawn range runs from the start of the first hit character's range to the end
   of the last one's (UTF-16, end exclusive). Draw the glyph boxes of that range, merged into
   one rectangle per line. State `relocated`.
7. A quote whose skeleton is empty is `orphaned`.

The reference implementation is `src/thoughts_server/review_feedback_anchor.py` (`resolve`).
Kotlin: `java.text.Normalizer.normalize(cp, NFKD)` and `Character.getType`. Qt:
`QString::normalized(QString::NormalizationForm_KD)` and `QChar::category`.

Consequences:

- A card that follows its moved markers into another PDF, an answer edit elsewhere, or a
  rebuild all keep the highlight, as long as the passage text still exists in the answer.
- If the passage's own text is edited, the highlight becomes `orphaned`. Nothing is deleted
  automatically.
- The search is limited to the card's own answer. Text outside the answer areas never matches.

## 5. Merge semantics (server, normative)

Records are mutable. A write wins by priority (`revision`, then `updated_at`, `origin_id`,
`operation_id`), compared with the stored record. `base_revision` is not checked. Losing writes
get outcome `superseded`: the operation is acknowledged, the client's queue moves on, and the
client receives the winner through `/v1/changes`. A retry of the same `operation_id` is a
`duplicate`.

Two extra rules:

1. **Immutable fields.** `card_id`, `anchor`, `created_at` and `created_origin_id` are taken
   from the stored record on every later write. A client that sends different values does not
   change them. The operation log keeps the submission, so a retry stays a duplicate.
2. **Delete is sticky, in every arrival order.** The full priority is (`revision`,
   `deleted`, `updated_at`, `origin_id`, `operation_id`), where a delete (`true`) ranks above a
   non-delete (`false`). At equal revision, a delete therefore beats a non-delete whatever
   their times and whichever reaches the server first. Two deletes, or two non-deletes, of
   equal revision are compared by `updated_at` as usual. A non-delete revives a deleted record
   only with a strictly higher revision: Undo of a delete is an ordinary write with
   `revision = tombstone revision + 1` and `deleted: false`, with no `deleted_at`.

Offline cases:

| device A | device B | result |
|---|---|---|
| creates F1 | creates F2 on the same passage | both kept: ids are random, duplicates are allowed |
| edits comment (rev 2, 11:00) | edits polarity (rev 2, 11:30) | B's whole record wins: later `updated_at` at equal revision. The fields are not merged. |
| deletes (rev 2) | edits comment (rev 2, earlier, later or at the same time; arriving before or after the delete) | stays deleted |
| deletes (rev 2), then Undo (rev 3) | – | restored |
| edits twice offline (rev 2, then rev 3) | deletes (rev 2) | A's rev 3 wins and the highlight stays: a higher revision always applies. This is accepted as rare. |

Cascade: deleting a card (a review event with `rating: "delete"`) or its document does not
tombstone its feedback, so the study data is retained. Clients and agent output hide feedback
whose card is deleted or unknown locally. If the card is restored, its feedback shows again.

## 6. Client UI rules (desktop and phone must match)

### 6.1 When highlights are visible

- They are drawn **only on the card under review**, and **only while that card's answer is
  visible**:
  - the review card view after Reveal, while the blue frame shows (until Hide, Next or leaving
    the review);
  - Set knowledge (it reveals the answer);
  - an in-place review started from the reader's Review Cards list, under the same rules.
- They are never drawn while the answer is masked, in normal reading, in the card browser, in
  Edit card, in exports or printing, or for any other card's answer. They are not annotations
  and are never written into the PDF.
- If two cards share a passage, only the feedback of the card under review is drawn.

### 6.2 Creating

- In an answer-visible state, a text selection qualifies when the centre of every selected
  line rectangle lies inside one of the card's current answer regions, each expanded by 0.01
  in normalized units. The selection may span pages if the answer does.
- A qualifying selection adds three actions at the start of the platform's selection menu or
  toolbar: **Like**, **Dislike** and **Comment…**.
  - Like and Dislike create the record at once with an empty comment, clear the selection, and
    draw the highlight.
  - Comment… opens the editor (§6.3) for a new record, with Like preselected. Nothing is saved
    until Save.
- A selection that does not qualify shows no feedback actions. The other selection actions are
  unchanged.
- Creation records `quote`, `pdf` and `segment_*` as described in §3, with `created_at` =
  `updated_at` = now (UTC, millisecond precision, `Z`) and `revision` 1.

### 6.3 Showing and editing

- Colours: a translucent fill plus a 2 px underline under the passage's text lines. When the
  comment is not empty, add a small filled dot (6 px) at the end of the last line.

  | | light theme | dark theme |
  |---|---|---|
  | positive fill / underline | `#2E7D32` at 22% / `#2E7D32` | `#81C784` at 28% / `#81C784` |
  | negative fill / underline | `#C62828` at 22% / `#C62828` | `#EF9A9A` at 28% / `#EF9A9A` |

  The positive underline is solid and the negative underline is dashed, so the two differ
  without colour.
- A tap or click on a highlight opens the editor anchored to it. It shows:
  - the quote, limited to 3 lines;
  - a two-state Like / Dislike toggle;
  - the comment box: multi-line, at most 4000 characters, placeholder "What do you like or
    dislike here?";
  - "Created <date>" and, when it differs, "edited <date>";
  - **Save**, **Delete** and Cancel.

  Save writes a new revision only if the polarity or the comment changed.
- Delete writes the tombstone at once, without a confirmation, and shows "Feedback deleted ·
  Undo" for 5 s (Android snackbar, desktop status message or inline banner). Undo writes
  revision + 1 with `deleted: false` (§5).
- Where highlights overlap, the most recently created one is on top, and a tap opens the top
  one.
- The answer-visible card panel shows a compact count, "2 liked · 1 disliked". Tapping it lists
  the card's feedback, ordered by position in the answer (orphaned ones last), and selecting an
  item opens its editor. The count is hidden when the card has no feedback.

### 6.4 Orphaned highlights

- They are not drawn on the page.
- They appear in the list from §6.3 under the heading "Not found in the current answer", with
  their quote, polarity and comment. Each can be opened, edited or deleted. They are never
  deleted automatically.
- The count shows "(1 not found)" after the totals when there are any.

### 6.5 Agent and copy text

The review card ⋮ "Copy for agent" output adds this section after the existing card lines,
and only when the card has non-deleted feedback. The format is identical on both clients:

```
Answer feedback:
- Liked: "the KL term keeps the policy close" — Clear explanation.
- Disliked: "β is set to 0.1" — Should be 0.05 in our setup. (not found in the current answer)
```

- One line per record, in created_at order.
- The quote has its whitespace runs collapsed to one space and is cut at 300 characters with
  "…".
- `— <comment>` is added only when the comment is not empty, with its newlines replaced by
  spaces.
- The `(not found in the current answer)` suffix appears only when the client's last
  resolution gave `orphaned`.

## 7. Agent exposure (review index, MCP, thoughtsctl)

Every card object returned by `search_review_cards`, `list_review_cards`, `thoughtsctl
search-review-cards` and `thoughtsctl list-review-cards` gets a `feedback` array: the card's
non-deleted records in `created_at` order, or `[]` when there are none.

```json
"feedback": [
  {
    "feedback_id": "5b0f6c1e-2a47-4d1b-9c35-7e0a4f2b9d13",
    "polarity": "negative",
    "quote": "the KL term keeps the policy close",
    "comment": "This sign is wrong: the KL term is subtracted.",
    "created_at": "2026-10-20T10:14:03.512Z",
    "updated_at": "2026-10-20T10:20:41.007Z",
    "origin": "android-pixel",
    "anchor_state": "pdf_changed",
    "page": 7,
    "segment_id": "0",
    "source_changed": false
  }
]
```

- `origin` is `created_origin_id`. `page` is the page of the first anchor region.
- `anchor_state` is `exact` when `anchor.pdf.pdf_sha256` equals the card's current
  `primary.pdf_sha256`. Otherwise it is `pdf_changed`, or `relocated` / `orphaned` when the
  index ran §4 on the current PDF.
- `source_changed`: `true` when the anchor has `segment_content_sha256` and the card's current
  segment with the same `segment_id` has a different `content_sha256`; `false` when they are
  equal; `null` when it cannot be known.
- Deleted feedback is excluded. For the study, an optional `include_deleted` flag adds deleted
  records with `deleted_at`.

## 8. Conformance tests

Server (`tests/test_review_card_feedback.py`, all implemented):

1. The capability is advertised. A record syncs through `/v1/changes` and `/v1/projections`,
   unknown fields are kept, and a retry is a duplicate.
2. Unknown fields inside `anchor` and `regions` are kept.
3. Negative feedback with several regions on several pages and no segment is accepted.
4. A higher revision wins. At equal revisions the later `updated_at` wins, in either arrival
   order. A stale revision is superseded.
5. Delete is sticky against a concurrent edit with the same revision, in both arrival orders
   and whether the edit is earlier, later or exactly as late. Undo with revision + 1 restores.
   A later delete with a higher revision applies. Equal-revision deletes break ties by
   `updated_at`.
6. `card_id`, `anchor`, `created_at` and `created_origin_id` stay those of the first write. The
   log keeps the submission, and its retry is a duplicate.
7. Every invalid field from §2 gives 422, and so does every missing required field.
8. Feedback for a card unknown to the server is accepted.
9. `/v1/document-sync` carries exactly the non-deleted feedback of the target PDF's cards.
10. Feedback is not in the build review snapshot and has `render_impact` `none`.
11. The shared anchoring vectors (§9) match the reference algorithm.

Clients (both platforms, unit tests unless noted):

- C1. Payload codec: a record from the §2 example round-trips byte-equivalent, including unknown
  top-level, `anchor` and `regions` fields.
- C2. Uploads wait for `review_card_feedback_v1`: queued without the capability, sent once it
  is advertised.
- C3. Local merge uses the same priority and sticky-delete rule as §5. Use the table in §5 as
  test cases, so that local results match what the server will return.
- C4. Re-anchoring (§4), on fixed answer-text strings:
  - (a) same sha → `exact`;
  - (b) text shifted, quote unique → `relocated` at the right range;
  - (c) quote appears twice, prefix/suffix pick the second;
  - (d) whitespace, line-break and soft-hyphen differences match;
  - (e) the text was hyphenated at the line end only in the new PDF and matches on the hyphen
    retry;
  - (f) the quote was edited → `orphaned`;
  - (g) the quote is outside the card's regions → `orphaned`.

  Use the shared vectors in `protocol/fixtures/review-card-feedback-anchoring.json` (§9) so
  that both clients give identical results.
- C5. Visibility: no highlight is drawn before Reveal, after Hide, for another card, in plain
  reading, or in the card browser. They are drawn after Reveal and after Set knowledge
  (instrumented on Android, mainshelltest on desktop).
- C6. Selection qualification: inside the answer → Like/Dislike/Comment… present; partly
  outside → absent.
- C7. Like creates `positive` with an empty comment. Comment… creates nothing until Save. An
  edit increments the revision and keeps `created_at`. Delete plus Undo gives revision n+2 with
  `deleted: false`.
- C8. Orphaned records are listed under "Not found in the current answer" and can be deleted.
- C9. Copy for agent: the exact §6.5 text for liked + disliked + orphaned.
- C10. Agent JSON (desktop): each card has `feedback` with the §7 fields. Deleted records are
  excluded, and `[]` is returned when there are none.

## 9. Shared anchoring vectors

`protocol/fixtures/review-card-feedback-anchoring.json` lists cases as `{name, answer_text,
quote: {exact, prefix, suffix}, same_pdf, expected: {state, start, end}}`. `start` and `end`
are UTF-16 offsets into `answer_text` of the drawn range (`null` for `exact` and `orphaned`).
Both clients run every case through their §4 implementation, with the answer text given
directly, and must produce exactly `expected`.

## 10. Where it goes in the clients (as of desktop be70ce8a6, Android db86f2a6)

These are pointers only. Line numbers drift.

Desktop (`~/repos/thoughts-desktop`):

- **Record logic**: a new `Thoughts::ReviewCardFeedback` namespace modelled on
  `part/linkoverridestore.{h,cpp}`: `entityType()`, `collection()`, `capability()`, `create`,
  `edit`, `remove`, `undo`, `newer` (§5 priority plus sticky delete), `validationError` and
  `enqueue` (`base_revision` = revision − 1).
- **Sync** (`part/fedorarepository.cpp`):
  - `holdUnsupportedOperations`: a new `m_reviewCardFeedbackSupport` flag, read from the
    `ready()` capabilities.
  - `retainAcknowledgedProjections`: add the type to the whitelist and handle tombstones.
  - The superseded-result filter.
  - `applyDocumentSnapshot` / `documentOperations`: scope to the target PDF's cards.
  - Pulled `/v1/changes` and `/v1/projections` already store unknown types generically.
- **Review store** (`shell/reviewstore.cpp`):
  - `ReviewStore::records()`: add the type to the outbox-overlay list.
  - `writeServerJson`: a `review-feedback/` folder branch. Without it, the final else turns
    the record into `review_exclusion`.
  - `refreshQueryDatabase`: a new table
    `review_feedback(feedback_id PK, card_id, polarity, quote, comment, created_at, updated_at, deleted, record_json)`,
    and bump the source-hash prefix `"3:"` → `"4:"`. Also update `tools/review-query/README.md`.
- **Agent output**:
  - `shell/reviewknowledge.cpp` `cardJson`: add `feedback` (§7). It must also work when the
    table is missing (an old index).
  - `thoughtsctl list-review-cards` (`ReviewStore::sourceCardCommand`, list branch): add
    `feedback`.
  - `tools/mcp/server.py` `search_review_cards` / `list_review_cards`: document `feedback` in
    the docstrings, and test it in `tools/mcp/test_server.py`. Then copy the server to
    `~/plugins/thoughts/scripts/server.py`, with a backup.
  - `shell/reviewdocumenttools.cpp` `reviewCardPromptFields`: add the §6.5 section.
- **UI**:
  - `part/pageview.cpp`: paint the highlights next to `paintReviewMask` /
    `paintAgentProposalOverlays`, only in the revealed review state.
  - Selection menu: `mouseReleaseEvent` text-selection menu, before "Add Review Card…", only
    while a review card's answer is revealed.
  - Bridge: `Shell::presentReviewCard` → part (new `showReviewFeedback(cardId, items)` /
    `clearReviewFeedback()`), driven by `reviewoverview.cpp` `toggleReveal` / `hideAnswer` /
    `revealAnswer` / `updateAnswerControls`.
  - Colours: §6.3, with dark mode detected as elsewhere (`palette().color(QPalette::Window).lightness() < 128`).
- **Tests**:
  - `autotests/reviewcardfeedbacktest.cpp`: C1, C3, C4 with the fixture, C7.
  - `parttest`: `testReviewCardFeedbackWaitsForServerSupport` and
    `testDocumentSyncScopesReviewCardFeedback`.
  - `reviewstoretest`: the query DB table.
  - `reviewknowledgetest` and `tools/mcp/test_server.py`: C10.
  - `mainshelltest` (`--qtest` on the builder): C5, C6, C9.

Android (`~/repos/thoughts-android`, `app/src/main/java/com/gitlab/mudlej/MjPdfReader`):

- **Store**: `data/review/ReviewCardFeedback.kt`, modelled on `data/links/PdfLinkOverrides.kt`.
  It keeps a local JSON store `reviews/card-feedback.json` through `ThoughtsLocalStore`, with
  entries carrying `pending` and `serverKnown`, and provides `create` / `edit` / `remove` /
  `undo` / `pendingUploads` / `nextRevision` / `installServer`.
- **Sync**:
  - `data/sync/ReviewCardFeedbackSync.kt`: an outbox plan modelled on
    `PdfLinkOverridesSync.kt`, gated on `review_card_feedback_v1`. Skip targeted Syncs and
    supersede older unsent operations of the same id.
  - `FedoraRepository`: `captureReviewCardFeedback` next to `capturePdfLinkOverrides`, and
    `installReviewCardFeedback` in `refreshReviewProjections`.
  - `ThoughtsSyncDao`: a `reviewCardFeedbackProjections()` query.
  - `documentScopedEntityKeys`: scope to the target PDF's cards.
- **Cache**: keep feedback out of the Gson-cached `ReviewCard` (load it from its own store).
  If it is ever cached there, bump `ReviewSnapshotCodec.VERSION` / `DASHBOARD_VERSION` and add
  null guards.
- **UI**:
  - `ui/reader/ReviewSessionController.kt`: `showAnswer` / `reveal` / `hideAgain` /
    `setCardMasks` drive a new `PDFView.setReviewFeedback(byPage)` / `clearReviewFeedback()`
    (AndroidPdfViewer `PDFView.java`, drawn after `drawReviewMask`, only when revealed; tap hit
    test to open the editor).
  - Selection toolbar: `InlineAnnotationActionController.showSelectionActions` adds
    Like / Dislike / Comment… while a review answer is revealed. Get regions from
    `pdfView.getHighlightRequest()` → `reviewSelectionRegions(...)`, which gives 1-based
    normalized top-left rects (check `pageIndex` + 1).
  - Answer text for §4.1: `ReviewAnswerTextLoader.regionsText`.
  - Copy for agent: `ReviewCardAgentDescription.reviewCardPromptFields` gets the §6.5
    section.
  - Colours: `values/colors.xml` and `values-night/colors.xml`
    (`review_feedback_positive` #2E7D32 / #81C784, `review_feedback_negative` #C62828 / #EF9A9A).
- **Tests**:
  - Unit: `ReviewCardFeedbackTest.kt` (C1, C3, C7), `ReviewCardFeedbackAnchoringTest.kt` (C4,
    reading the fixture JSON from `../thoughts-server/protocol/fixtures` or a copied test
    resource), `ReviewCardFeedbackSyncTest.kt` (C2), and `ReviewCardAgentDescriptionTest.kt`
    (C9).
  - Instrumented: `ReviewCardFeedbackInstrumentedTest.kt` (C5, C6, C8).
