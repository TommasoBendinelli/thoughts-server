<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Protocol compatibility contract

`openapi.yaml` is the versioned wire contract for server version `0.1`.

All protected requests use a per-device bearer token over HTTPS: the server's public origin (`THOUGHTS_PUBLIC_BASE_URL`, for example `https://thoughts.example.org`) or a private Tailscale address.
Repeated failed authentication from one client address is answered with `429 Too Many Requests` and `Retry-After`; on `/v1/notifications` the WebSocket handshake is refused the same way.

Clients must persist operations before attempting network delivery and may retry the same operation indefinitely.

The server deduplicates by `operation_id` and rejects reuse of a device sequence by a different operation.

Clients advance their durable change cursor only after applying the complete returned page locally.

The polled listings carry an `ETag` and answer `304 Not Modified` to a matching `If-None-Match`: `GET /v2/sync/state`, `GET /v2/library`, `GET /v1/library`, `GET /v1/library/projects` and `GET /v1/render-state` (whose tag names the render state, not the `observed_at`, `storage` and `server_summary` fields added to every answer). Sending it is optional; without it the same bytes are returned as before.

Builds. When several builds of the same target wait, the server builds only the newest. Each older one it covers (same toolchain, no repair or review-card mode, frontiers not older, and for `all` a document list the newest includes) is not built and not ended: it stays `queued`, `result.superseded_by` names the covering build, and `progress.detail` says which build it waits for. When the covering build ends `succeeded`, `attention` or `failed`, every build it covered ends at the same moment with the same `status`, `result` (plus `superseded_by`, the build that ran), `error` and completion time, so a wait on a covered build (`GET /v2/builds/{id}?wait_ms=`) returns then, with that outcome; its output (`/v2/builds/{id}/events`) is the covering build's. No status other than `queued`, `running`, `succeeded`, `attention`, `failed` and `cancelled` is used, and a build ends `cancelled` only when a client cancelled that build: if the covering build is cancelled or no longer exists, the builds it covered wait for the build that covered it, or go back to the queue to be built themselves. Cancelling a covered build cancels only that build. A trace linked to a covered build gets one informational event (`state: "queued"`, no `error_code`, `metadata.superseded_by`) and later the terminal event of the outcome.

A build request beyond the Library's queue limit is refused with `429`, `error_code: "build_queue_full"`, `queued_builds`, `max_queued_builds` and `Retry-After`; nothing was queued, and the same request can be sent again later. The limit counts builds that will run: a covered build, and a build of a target whose newest waiting build will cover it, count as one with that build.

Build repair and review-card generation were removed from the server. A build request with `repair_mode: "codex"` is refused with `409`, `error_code: "feature_removed"`, `feature: "build_repair"` and a `detail` that says so; one with `repair_mode: "review_cards"` or a `review_generation` member is refused the same way with `feature: "review_card_generation"`. Nothing is queued. A request with `repair_mode: "none"`, or without the field, is an ordinary build. Build records always carry `repair_mode: "none"`. The server no longer lists the capability `codex_build_repair_v1`. Review cards re-anchor by their source markers and, without them, by their saved answer text (`review_reanchor_v1`); no agent takes part.

Uploads. When the server has a storage limit for the Library, `POST /v1/objects` answers `507` with `error_code: "library_storage_quota_exceeded"`, `quota_bytes` and `used_bytes` for an object that would exceed it; an object the Library already stores is always accepted.

Objects are immutable and addressed by the lowercase SHA-256 of their complete bytes.

Source changes reference an uploaded base object and incoming object.

An explicit `conflict` response means that the canonical source was not modified.

Open conflicts are re-checked with the server's current merge rules whenever `GET /v1/conflicts` is called (any `status`). One whose stored base, server and incoming versions now merge cleanly, and whose result also merges cleanly onto the file if it changed since, is resolved by the server like a resolution with that text: a new source revision from the internal device `thoughts-conflict-recheck`, the conflict listed as `resolved`. One clash is one conflict: an upload with the same path, base, current and incoming versions as an open conflict answers that conflict's `conflict_id` and `merged_object_sha256`. Older records of one clash keep the newest open; the others get status `duplicate`, are no longer listed as `unresolved`, can still be resolved or discarded by their ID, and take the status the open one gets when it is resolved, discarded or superseded.

Unknown payload fields must be retained when a client rewrites an entity.

PDF link overrides (`pdf_link_override` records) are specified in `pdf-link-overrides.md`.

Agent conversations (`agent_conversation` records: Claude Code and Codex conversations linked to a Project) are specified in `agent-conversations.md`.

Review card feedback (`review_card_feedback` records: liked or disliked passages of a card's answer) is specified in `review-card-feedback.md`.

Document feedback (`document_feedback` records: liked or disliked passages of any Library PDF, kept with the PDF snapshot they were made on) is specified in `document-feedback.md`.

Importing a PDF from a URL into a Project (`POST /v1/library/url-imports`, capability `library_import_pdf_v1`: the server downloads the PDF and makes the desktop's Import document changes) is specified in `library-url-imports.md`.

Publishing and unpublishing Project documents (Library-project action `set_document_published`; cards of unpublished documents are not offered for review) is specified in `project-document-publication.md`.

Retiring review cards (`review_event` queue actions `retire`, `restore` and `undo_retire`) is specified in `review-card-retire.md`.

Merging review cards of one document into one card on the server (`POST /v1/review-card-merges`, capability `review_card_merge_v1`: the records the desktop's Merge cards… sends, made and applied in one transaction) is specified in `review-card-merge.md`.

Moving a review card onto a pending agent proposal's proposed text (`POST /v1/review-card-pending-moves`, capability `review_card_pending_moves_v1`: the move waits for the proposal and the server makes it when the user accepts it, or drops it when the proposal is rejected or withdrawn) is specified in `review-card-pending-moves.md`.

Feedback version stamp: any record may carry a top-level `review_version` object, `{"pdf_sha256": "<64 lowercase hex>", "source_built": true|false}`, naming the PDF version the user was looking at when giving the feedback (review events, comments and replies, notes, card feedback, and any other user feedback). Clients omit the field when the hash is not known, stamp on create and edit, and not on delete. The server stores it as sent, also on otherwise strict record types, and PDF retention never removes a PDF named by any record's `review_version`. A record made in the HTML view may add `"html_edition_sha256"` to the stamp; retention keeps that HTML edition and its PDF (`docs/html-edition.md`).

Review cards made inside HTML attachment pages (capability `html_attachment_cards_v1`) have `primary.created_in: "html_attachment"`, `primary.html_attachment {path, sha256}`, `primary.text_quote`, optional `primary.page_state {hash}`, no PDF geometry, `source_authority: "client-v1"` and the terminal `source_link: {"state": "html_attachment"}`; an attachment version the owner PDF never linked to is refused with 409 `error_code: "html_attachment_unknown"` (`docs/HTML_ATTACHMENTS.md`). A `created_in` other than absent, `html` or `html_attachment` is refused.
