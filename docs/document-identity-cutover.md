<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Permanent document identity cutover

A document receives a random UUID at local registration, before its first publication.
The PDF SHA-256 identifies its bytes and remains separate from this UUID.
Source manifests persist the aggregate UUID and each source document UUID before rendering.
Filename and source path fields locate files and rendering inputs; they do not generate document identifiers.

Imports carry the client's UUID to Fedora, and both clients check the returned UUID before acknowledging publication.
A renamed display title, project move, rebuild, or Sync keeps the document UUID.
When a placement moves to a different assembled PDF, `pdf_document_id` identifies that placement's PDF while `document_id` keeps its logical owner.
The PDF fingerprint still determines whether the stored coordinates apply.

## Coordinated migration

This is an offline hard migration with no runtime aliases for the former identifiers.
Do not install one updated production client while the other clients and server still use the old data.
Stop the API, all workers, and every client before taking consistent backups of the database, Library, object store, and client data, including SQLite WAL files.
Keep them stopped throughout the cutover.

1. Upgrade the server schema with `alembic upgrade head`.
2. Preview the server data migration with `thoughts-admin migrate-document-identities`.
3. Apply it with `thoughts-admin migrate-document-identities --commit --receipt /path/to/identity-receipt.json`.
4. Apply that same committed receipt to each complete client export with `python tools/migrate-document-identities.py /path/to/client-export /path/to/identity-receipt.json --check`, followed by `--commit`.
5. Restore the migrated client data and install matching Desktop and Android builds before restarting the services and clients.
6. Perform the first normal Sync explicitly and verify the Library and existing annotations.

The client export must include the Library, review records, pending journals, source project manifests, and every application SQLite database.
For Desktop, this includes both the documentation workspace and the Qt application data `thoughts-server` directory containing `desktop-sync.sqlite`.
For Android, include `files`, `no_backup`, and `databases` from the stopped app, preserving their relative paths and the original absolute paths stored in records.
Dereference the active Library link when making an export, and preserve original paths when restoring it.
For stores already arranged under a common directory, repeat `--include /absolute/store/path` to scan only those selected files or directories in one invocation.
Select active metadata, source manifests, and databases rather than historical backup folders or unrelated research files.
Use `--registry /absolute/path/document-identities.json` when the split export does not expose the Library directory directly beneath its root.
Do not migrate split stores independently, because locally pending imports must receive the same UUID in every store.

The server saves the receipt in the database and re-exports the same mapping on retries.
The client saves its additional local registration plan before rewriting data, preserves changed files and SQLite backups beneath `.document-identity-cutover-backup`, and records completion only after all updates succeed.
A failed attempt can be retried with the same receipt and the same complete export.
Never run a different receipt over a partially migrated client.
Restore the complete backup if abandoning the cutover.

The migration preserves annotation and operation identifiers, revisions, geometry, fingerprints, review history, and undelivered operations.
Project operations already acknowledged by the server are removed from the client's pending list using the exported acknowledgement evidence.
Active placement manifests are re-ingested with their new identifiers, while their historical objects remain available.
The server rejects document changes carrying obsolete identifiers after cutover.

## Validation

The regression tests cover a pre-Sync manual review card surviving restart and publication, unchanged PDF relocation preserving page objects and undo state, registration before a first PDF exists, rejected identity collisions, repeatable server and client migration, retained placement coordinates, and pending imports stored in SQLite.
The Android instrumentation test runs only in `org.tbend.thoughts.sandbox` and checks publication without requesting a reader reload.
Production installation, data migration, and explicit Sync are separate from these tests.
