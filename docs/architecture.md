<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Architecture

## Authority boundaries

PostgreSQL is authoritative for mutable operations, entity projections, source revisions, conflicts, build jobs, and publication pointers.

The content-addressed object store is authoritative for immutable source bytes, PDFs, indexes, logs, and provenance objects.

The server worktree is a reconstructable cache rather than an independent authority.

JSONL sidecars, review files, and annotated PDFs are generated interoperability and recovery views.

Thoughts on Mac and Android retain local databases, cached PDFs, and crash-safe outboxes so their interactive editing paths do not wait for the network.

## Operation synchronization

Each device assigns a durable device sequence and globally unique operation ID before attempting delivery.

The server accepts a retry only when every identity and payload field is identical to the stored operation.

Reusing an operation ID or device sequence with different content is a conflict.

The server assigns a monotonically increasing sequence used as the download cursor.

Legacy imports retain their exact source path and record location as operation provenance without changing the imported payload.

Clients apply a complete change page transactionally before advancing their durable cursor.

Comment projections are keyed by entity type, document collection, and stable comment ID.

That collection boundary permits a live destination comment and a `source_moved` tombstone in the old document to coexist safely.

Global resolutions use their own entity type and collection.

Revision, update timestamp, origin, and operation ID provide deterministic per-entity winner ordering while the complete operation log retains losing inputs.

An absent record is never interpreted as deletion.

## Immutable objects and publications

Uploads stream into a same-filesystem staging file, are flushed, verified against the declared full SHA-256, and are atomically renamed into a digest-derived path.

Object database records contain the digest, size, media type, and relative storage path.

The Library endpoint exposes `Publication` records whose membership comes from `.thoughts-library-index.json`.

`.thoughts-source-index.json` independently identifies the source-linked subset, while unlinked publications
remain ineligible for source selection, source editing, and targeted source rebuilds.

Publication activates only when the PDF bytes match the full hash recorded in the Library index, and the
filesystem Library index is regenerated as a projection of committed publication rows.

Clients download by digest and verify the complete content before local activation.

## Explicit source publication

Managed UTF-8 sources have a path, revision, byte hash, object hash, and explicit deletion state.

A publication contains only explicit path mutations prepared against each client's recorded revision and hash base.

Each mutation names the exact Fedora revision and hash it replaces, so a stale publication fails closed.

PostgreSQL locks the publication set and commits every mutation in one transaction.

An interrupted client retries the same durable publication identity and cannot partially duplicate a publication.

Source bytes are uploaded to the immutable object store and verified by complete SHA-256 before publication.

An unchanged local path fast-forwards to a newer Fedora revision, while a locally changed path is submitted against its recorded base for a deterministic merge or conflict.

The merge is `git merge-file`, which also stops at edits on adjacent lines; in LaTeX and Markdown sources (`.tex`, `.md`, `.markdown`) such edits are combined when no line is changed by both sides, neither edit touches a review-card or source-comment marker line, and every agent proposal comes out as a three-way merge of that proposal alone would leave it (`adjacent_edits.py`). A typical case is an agent proposal written next to a passage the user just edited or decided on another device.

Manifest absence never means deletion, and no client publishes a whole-tree snapshot that can overwrite unrelated source changes from another device.

Explicit publication manages bounded UTF-8 sources, while every non-source binary or generated dependency needed by a target is uploaded by full SHA-256 and registered as an immutable build-input snapshot.

## No remote provider synchronization

Thoughts Server never fetches from, merges with, or pushes to Overleaf or any Git remote.

Canonical sources change only through explicit client source publication.

## Build lifecycle

A build key contains target PDF, canonical source frontier and snapshot hash, immutable build-input snapshot, canonical comment frontier, and toolchain identity.

The unique key coalesces identical requests, while a failed or cancelled job can be explicitly retried under the same identity.

The worker reconstructs the exact managed source frontier, materializes the selected build-input snapshot, and exports JSONL/review views from authoritative storage before invoking the renderer.

Reconstruction uses the job's recorded frontiers, rather than the latest projections, so edits accepted after enqueue cannot leak into that build.

A cross-process worktree lock protects the snapshot through rendering, and the latest canonical sources and generated views are restored afterward.

Only applied or cleanly merged operations advance a build frontier.

The worker records renderer progress, enforces a timeout, retains logs as immutable objects, and recovers interrupted running jobs after a process restart.

A successful publication requires every emitted PDF to match its source-index hash, and publication provenance records the build, source snapshot, input snapshot, and operation frontiers.

An unchanged output does not acquire newer provenance, and a failed target keeps the last-good PDF pointer while the Library exposes the failed build as stale.

Partial renderer success becomes `attention` with PDF-specific outcomes rather than an undifferentiated success.

## Deployment and security

Fedora runs the API, worker, PostgreSQL, backup, and integrity jobs as the locked `thoughts` service account.

The API binds to loopback and Tailscale Serve provides private tailnet HTTPS.
Public HTTPS reaches the same loopback API through a blind TCP relay VPS, WireGuard, and Caddy on the Fedora host (`deploy/relay/README.md`).

Each client receives a separately revocable bearer token whose digest alone is stored in PostgreSQL.

Systemd restricts writable paths to state, objects, and the reconstructable worktree.

Production refuses development builds until the Fedora renderer and headless Okular helper paths are configured.

Production schema creation is Alembic-owned; the API's automatic `create_all` behavior is enabled only for development and focused tests.

Database dumps and content-addressed objects are backed up together, and the restore drill uses a disposable database without touching production state.
