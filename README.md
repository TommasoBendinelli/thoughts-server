<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Thoughts Server

Thoughts Server is the server-side control plane for Thoughts on Mac and Android.

By **Tommaso Bendinelli**.

It preserves local-first reading and editing while making an always-on Fedora ARM64 host authoritative for annotations, explicitly published sources, rendering, immutable artifacts, review events, provenance, and backups.

The normal data path does not use Syncthing and the deployment does not use containers.

## Current implementation

The repository includes:

- an authenticated FastAPI service with idempotent operation batches and cursor-based change retrieval;
- stable per-entity projections with explicit deletion records and deterministic revision ordering;
- a SHA-256-addressed immutable object store with atomic publication and integrity verification;
- atomic, explicit, revision-aware source publications with deliberate versioned deletions;
- per-path source bases that fast-forward clean clients and merge or conflict dirty concurrent edits;
- immutable non-source build-input snapshots bound to exact build jobs;
- a persistent, deduplicating build queue with progress, cancellation, logs, and artifact publication;
- a separate durable source-anchor queue that updates comment metadata and invisible `\\CommentAnchor` source markers without rendering PDFs;
- legacy workspace import and JSONL/review export tools;
- Kotlin/JVM and Qt/C++ client SDKs;
- native Fedora Asahi bootstrap, systemd, backup, and integrity-check assets.

The worker produces a clearly marked development artifact when no renderer is configured.

Production deployment configures the existing documentation renderer and the Linux build of `thoughts-comment-sync`.

## Local development

Python 3.11 or newer and Git are required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export THOUGHTS_STATE_ROOT="$PWD/state"
export THOUGHTS_OBJECT_ROOT="$PWD/objects"
export THOUGHTS_WORKTREE_ROOT="$PWD/worktree"
export THOUGHTS_BOOTSTRAP_TOKENS='{"local":"replace-this-local-development-token"}'
.venv/bin/thoughts-server
```

Run the persistent workers separately:

```bash
.venv/bin/thoughts-worker
.venv/bin/thoughts-anchor-worker
```

The API listens on `127.0.0.1:8080` by default.

Interactive API documentation is available at `/docs`.

Run validation with:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

SQLite is supported for local development and focused tests.

PostgreSQL is the production authority and is exercised after connection to the Fedora server.

Production sets `THOUGHTS_AUTO_CREATE_SCHEMA=false`; systemd runs Alembic before the API starts.

## Device provisioning

Initialize the schema and create independently revocable device credentials:

```bash
.venv/bin/thoughts-admin init-db
.venv/bin/thoughts-admin create-device thoughts-mac --name 'Thoughts on Mac'
.venv/bin/thoughts-admin create-device thoughts-source-sync --name 'Thoughts Mac Source Publisher'
.venv/bin/thoughts-admin create-device thoughts-android --name 'Thoughts on Android'
```

The generated token carries 256 random bits, is printed once, and only its SHA-256 digest is stored.

Bootstrap tokens in `THOUGHTS_BOOTSTRAP_TOKENS` and explicit `--token` values must contain at least 32 characters; the server refuses to start otherwise.

Every protected request uses `Authorization: Bearer <token>`.

Credentials can be revoked or rotated independently:

```bash
.venv/bin/thoughts-admin revoke-device thoughts-android
.venv/bin/thoughts-admin rotate-device thoughts-android
```

## Sync trace inspection

Clients send the durable run UUID in `X-Thoughts-Trace-Id`; the server validates and echoes it on every response and carries it into source publications, build jobs, operations, and resulting publications.

Inspect every durable server record for one run with:

```bash
.venv/bin/thoughts-admin inspect-trace 3392cde0-d453-4d95-bb74-c6fcc7f531d6
```

The command prints structured JSON and never includes source contents or credentials.

## Comments whose source anchor was removed

A comment whose marker disappears from canonical source without an explicit Delete stays alive with scope `{"state": "needs_attention", "reason": "anchor_removed"}`, its last `source_file`/`source_line`, and `anchor_kind`, `anchor_mirror`, `anchor_removed_at`. Clients show it under Issues as "Anchor removed"; a unique marker reappearing in the project relinks it. Deleting a document's own entry source still retires its comments as a document deletion.

Earlier releases deleted such comments instead. List them, read-only, with:

```bash
.venv/bin/thoughts-admin report-auto-deleted-comments [--format json|tsv] [--docs-root PATH]
```

The report restores nothing. Client edit-path auto-deletions carry no reason and cannot be listed.

## Legacy migration

Dry-run the current workspace first:

```bash
.venv/bin/thoughts-migrate import --docs-root ~/Documents/docs
```

The dry-run validates records and reports counts without committing database rows or object files.

Commit only after the report contains no errors:

```bash
.venv/bin/thoughts-migrate import --docs-root ~/Documents/docs --commit
```

Export server projections into recovery-compatible JSONL and review files:

```bash
.venv/bin/thoughts-migrate export --destination ./recovery-export
```

The importer retains unknown fields in every comment and review payload.

It does not mutate the source workspace.

Reconcile a previously migrated Library against its verified source and Library indexes without
re-importing comments or reviews:

```bash
.venv/bin/thoughts-migrate reconcile-library --docs-root /srv/thoughts/worktree
.venv/bin/thoughts-migrate reconcile-library --docs-root /srv/thoughts/worktree --commit
```

The command is dry-run by default, never treats an absent index entry as deletion, and commits only
after every indexed PDF passes filename, kind, source-linkage, PDF-magic, and full SHA-256 validation.

## Source publication

Desktops publish sources with the native publisher bundled in the Thoughts
desktop app, through the revision-aware `/v1/source-publications` API. This
repository no longer ships a Python source client.

Pending publication identity is staged durably before submission, and the complete mutation set commits atomically on Fedora.

The publisher reads Fedora metadata only to calculate upserts and explicit deletions; it never pulls files into or watches the Mac authoring tree.

There is no production launchd watcher or bidirectional filesystem reconciliation.

## Fedora Asahi deployment

Deployment files live under `deploy/fedora`.

The service uses a locked `thoughts` account and the following persistent paths:

```text
/srv/thoughts/app
/srv/thoughts/venv
/srv/thoughts/objects
/srv/thoughts/worktree
/srv/thoughts/state
/srv/thoughts/backups
```

PostgreSQL is installed and managed by Fedora.

The API binds only to loopback and is published to the tailnet with Tailscale Serve.

It can also be published at a public HTTPS name (`THOUGHTS_PUBLIC_BASE_URL`, default `https://thoughts.example.org`) through a blind TCP relay VPS and Caddy on the Fedora host; see `deploy/relay/README.md`.

The future SSH deployment begins with the read-only preflight script and does not install packages or change services until its report has been reviewed.

## Security and recovery rules

- A missing record never means deletion.
- A source publication either advances the complete declared source set or leaves it unchanged.
- Uploaded and published objects are verified by full SHA-256.
- Device operations are idempotent and device sequence reuse is rejected.
- The operation log is retained even when an operation loses deterministic conflict resolution.
- PDFs and generated JSONL files are interoperability artifacts rather than the only copy of mutable state.
- The API must not be exposed directly to the public internet; public HTTPS terminates in Caddy on the Fedora host, and the API trusts forwarded client addresses only from loopback.
- Ten failed bearer authentications from one client address within five minutes block that address with `429` for fifteen minutes (`THOUGHTS_AUTH_FAILURE_LIMIT`, `THOUGHTS_AUTH_FAILURE_WINDOW_SECONDS`, `THOUGHTS_AUTH_LOCKOUT_SECONDS`, `THOUGHTS_AUTH_TRACKED_CLIENTS`).

## License

GPL-2.0-or-later: see `LICENSE`; the full licence text is in `LICENSES/GPL-2.0-or-later.txt`.
Third-party material and dependencies are listed in `THIRD-PARTY-NOTICES.md`.
