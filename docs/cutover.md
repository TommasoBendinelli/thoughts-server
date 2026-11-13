<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Clean-break cutover checklist

No cutover is authorized merely by installing the server.

The existing Thoughts applications continue using the current workflow until their SDK integrations and user-visible acceptance tests pass.

## Server preparation

1. Run the Fedora preflight script through SSH and record the Fedora release, Asahi kernel, ARM64 architecture, storage, memory, SELinux mode, Tailscale version, and available packages.
2. Review the report before running `install-native.sh --apply`.
3. Validate the PostgreSQL migration, API health, systemd hardening, local bind address, and Tailscale-only HTTPS exposure.
4. Seed the reconstructable worktree, then register immutable build-input snapshots for every source-linked target from fresh LaTeX recorder files.
5. Build `thoughts-comment-sync` natively and validate the complete TeX, SyncTeX, qpdf, and Ghostscript toolchain.
6. Enable the production renderer variables and verify one targeted build before accepting client jobs.

## Data migration

1. Run `thoughts-migrate import` without `--commit` against a current read-only workspace snapshot.
2. Compare reported counts with both the complete Library index and the source-linked subset, plus canonical comments/reviews.
3. Exercise import and export against a temporary database and compare parsed JSON records, not merely filenames.
4. Schedule a short mutation freeze for both readers and source editing.
5. Drain or explicitly resolve all old phone render/source requests and Syncthing conflicts.
6. Import the final delta with `--commit` and verify every immutable object.
7. Dry-run and commit `reconcile-library`, then require its linked and unlinked counts to match the verified Library index.
8. Generate the managed source manifest and perform one explicit revision-aware source and build-input publication.
9. Create separate device tokens and store the Mac token in Keychain and the Android token in platform-protected storage.

## Client activation

1. Integrate the Qt SDK into Thoughts without changing its immediate local annotation behavior.
2. Integrate the Kotlin SDK into Thoughts for Android with Room and WorkManager ownership of local application state and retries.
3. Test offline comment additions, edits, replies, moves, deletions, review events, and source drafts on real devices.
4. Test reconnection in both device orders and verify server, JSONL export, open Thoughts for Mac state, and Thoughts for Android state converge.
5. Verify queued, accepted, building, attention, failed, stale-last-good, downloading, and locally unsent states in the actual UI.
6. Verify a targeted phone build completes while the Mac is off and activates only after the PDF hash matches.
7. Perform a backup restore drill and a fresh-process Okular test before declaring the server authoritative.

## Retirement

After acceptance, disable the old documentation source watcher and any Syncthing request/receipt folders in one maintenance window.

Retain their final contents read-only until multiple server backup cycles and restore drills pass.

Syncthing may remain installed for unrelated folders, but it must not dual-write MJ/Okular canonical state after cutover.
