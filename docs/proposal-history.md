<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Proposal history

The `agentProposedEdit` macro has optional `[created=<ISO 8601 timestamp with timezone>]` metadata followed by four braced content arguments.
The source remains the authority for pending proposal content and its creation timestamp.
New proposals require a timestamp; revisions and moves preserve it.
The legacy migration uses earliest first-seen history, or the migration observation time when no earlier record exists, without changing proposal IDs or content.
History is an append-only set of `proposal_event` records in the global `proposals` collection, keyed by event ID and linked by the macro's stable proposal ID.
Each record preserves its rationale, original text, replacement text, source path, source hashes, observation time, and originating source operation when available.
No source files are reconstructed from these snapshots.

Events are `first_seen`, `changed`, `moved`, `accepted`, `rejected`, `deleted_from_source`, or `restored`.
Undated legacy snapshots still use explicitly labelled first-seen observations.
Dated source snapshots include `created_at`, which survives publication, partial decisions, and history synchronization.
Unchanged refreshes, source line shifts, rendering, and placement updates do not add content revisions.
Resolution history survives the removal of the macro, and undo adds a restoration event.

The server appends canonical events in the same transaction as successful single-file updates, atomic source publications, and conflict resolutions.
A verified exact replacement by the proposed or original text establishes acceptance or rejection, respectively.
Explicit server resolution operations also establish the action when the two alternatives are identical.
Other verified removals are classified as deleted from source.
Atomic moves preserve identity, and commented, malformed, duplicate, or still-present IDs do not establish deletion.
Conflicts and failed publications do not change canonical history.

Desktop records intermediate local observations in `comments/.history/proposal_events.jsonl`.
A prepared transaction journal protects local acceptance, rejection, and undo across an interrupted source save; recovery promotes its events only after verifying the intended resulting source hash.
Explicit Sync submits local events through the existing durable outbox, and the ordinary projection stream distributes both local and canonical events.
Local observations cannot claim canonical authority and never change the authoritative TeX state.
A local observation and its later server confirmation may both appear, labelled by their evidence source.
Workspace exports include the synchronized records in `comments/proposal_events.jsonl`.

The desktop overview uses shared date selection for comments, notes, and proposals.
The Created column uses comment creation timestamps and proposal source timestamps, falling back to explicitly labelled first-seen times only for undated legacy snapshots.
The update column uses `content_updated_at` for logical comments and annotation modification dates for PDF comments, with compatibility fallbacks.
Logical placement updates preserve `content_updated_at`, and proposal confirmation of the same source revision does not advance its content date.
The proposal history dialog includes accepted, rejected, and deleted proposals, with the saved text for every observation.

Deploy the updated server before enabling history synchronization in updated desktop clients.
The server advertises `proposal_history_v1`; no database table migration is required because events use the existing operation and entity tables.
Run `thoughts-admin backfill-proposal-history` to record first observations of existing canonical sources without editing TeX or rebuilding PDFs.
The command is idempotent and does not invent historical creation or resolution dates.
Earlier resolution jobs retain their existing audit records; this change does not infer a complete revision history from them.

## Partial review events

VS Code records prefix decisions as `partially_accepted` or `partially_rejected` while text remains pending, and uses the existing resolved kinds when no proposal remains.
The `review` payload preserves both selected prefixes, UTF-16 offsets, the remaining proposal, exact accepted or rejected text, the earliest recorded snapshot, and the decision timestamp.
An unchanged unique suffix can establish correspondence to the first-recorded prefix; ambiguous correspondence and whether it differs remain explicitly unknown.
First-recorded content is not labelled verified original agent output without generation evidence.

The editing context preserves the source before review, the rationale, and conversation IDs when available.
Generation instructions remain null when unknown.
Preference alternatives are candidates requiring context and preference validation; an unchanged acceptance alone is not a pair.

VS Code writes one immutable event per file under `comments/.history/proposal_events.d/`.
Desktop merges these with the existing journal, deduplicates by event ID, rejects conflicting records, displays partial decisions, and submits them through explicit Sync.
No client rewrites another client's event files.
The server advertises `proposal_partial_review_v1` and validates the selected text against the snapshot and UTF-16 ranges before accepting the event.
The existing projection and workspace export routes preserve the full payload.

Decisions initially have `source_state: editor_buffer`.
A `source_saved` event confirms an exact saved result, while `review_undone` and `review_redone` retain the original event and refer to its ID.
These local review events do not themselves resolve the canonical TeX source or claim successful publication.
Prepared editor transactions are recoverable only after the exact intended source hash is observed on disk.

## Decisions and removed proposals (phase 2)

Normative: `protocol/agent-proposals.md` section 10; fixtures `protocol/fixtures/agent-proposals/decisions.json`
(run by `tests/test_proposal_decisions_fixtures.py`). The server advertises `proposal_decisions_v1`.

Current proposal rows (`entity_type = proposal`, collection `proposals`) gain only additive fields:

- `status`: `pending`, `removed_outside` (live, `deleted: false`), or a retired status
  (`deleted: true`): `accepted`, `rejected`, `decided_in_parts`, `withdrawn`, `dismissed`,
  `document_deleted`, and the legacy `deleted_from_source`. `kind` carries the same value for
  `removed_outside` and retired rows; pending rows keep `first_seen`, `changed`, `moved`,
  `restored` or `source_saved`.
- `wrapper_sha256`: SHA-256 of the UTF-8 wrapper text, on every row the server writes from
  now on (the current wrapper while pending, the last verified one afterwards).
- `removal` (only while `removed_outside`): `operation_id`, `device_id`, `removed_at`
  (`YYYY-MM-DDTHH:MM:SS.mmmZ`), `source_line` (1-based, in `before_sha256`), `before_sha256`,
  `after_sha256` (`""` when the file was deleted), `looks_like` (`accept`, `reject`, `edited`,
  `file_deleted`; a hint, never a decision). It is dropped when the row leaves `removed_outside`.

Clients that only know `pending` stay correct: the phone hides every non-pending row and the
desktop does not read these rows. The phone source bundle manifest is unchanged.

Decision claims (`ProposalDecisionClaim`: `decision_id`, `proposal_id`, `action` = `accept` |
`reject` | `partial` | `withdraw`, `wrapper_sha256`, `source_file`, `decided_at`, `origin`) are an
optional `proposal_decisions` array (at most 500, unique `proposal_id` and `decision_id`) on
`POST /v1/sources/apply`, `POST /v1/source-publications` and `POST /v1/conflicts/{id}/resolve`.
They are part of the request identity: a retry with other claims is refused (409), a retry with
the same claims answers the stored outcomes. Responses carry `proposal_decisions:
[{decision_id, proposal_id, outcome}]` with `outcome` = `matched` | `matched_late` | `settled` |
`unmatched` (`SourceApplyResult.proposal_decisions`; for publications
`SourcePublicationResult.proposal_decisions`, while each result's own field stays null). The
field is null when the request carried no claims, or when an apply answered `conflict` (the
claims then travel with the conflict resolution). No claim is stored beyond the transaction
except its outcome in the request's operation (or publication) record, for replay.

A wrapper that vanishes without a claim or a server-applied resolution becomes
`removed_outside`. With `THOUGHTS_PROPOSAL_DECISIONS_STRICT` off (the default, compatible mode)
a removal whose `looks_like` is `accept` or `reject` is still retired as `accepted`/`rejected`.
Deleting a document's entry source retires its proposals as `document_deleted`.

Dismiss is the only client operation on a proposal row: `POST /v1/operations` with
`entity_type: proposal`, `base_revision` = the row's revision and the row's payload with
`status: dismissed`, `kind: dismissed`, `deleted: true`. Anything else, a row that is not
`removed_outside`, or a stale `base_revision` is refused with 409 ("The proposal is no longer
removed outside Thoughts"); a client drops the queued Dismiss then. Restore Proposal is a plain
source edit; `thoughts_server.proposal_decisions.restore_proposal` is the reference placement
algorithm and `attach_claims` the reference upload rule.

No table migration: rows are JSON payloads. Pending rows gain `wrapper_sha256` at the next
source upload (every reconciliation scans the canonical tree), or at once with
`thoughts-admin backfill-proposal-wrappers --commit` (idempotent; without `--commit` it counts).
Proposals retired before phase 2 stay retired; only removals observed afterwards can become
`removed_outside`.

