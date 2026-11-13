<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Source Git history on Fedora

The source history worker records the sources accepted by Fedora in a separate bare Git repository at `/srv/thoughts/state/source-history.git`.
The database and immutable object store remain authoritative for synchronization.
The worker never commits the mutable server worktree, edits notes, or pushes to a remote.

The first run checkpoints the currently saved sources.
It does not import older revisions into Git; those remain in the existing Thoughts source operation log and object store.
Subsequent runs create one commit for each accepted source publication that changes an eligible file.
Standalone source operations, including Android edits and conflict resolutions, are checkpointed individually.
A multi-file publication stays together, and multiple publications between timer runs retain their intermediate versions.
The commit message records the originating device, publication or operation identity, and source sequence.

The `thoughts-source-history.timer` runs the worker approximately every 30–35 seconds after the preceding run completes.
This records successful source saves, including saves followed by a failed PDF build.
It does not wait for the entire PDF download or global Sync operation to finish.
A failure is reported by systemd and retried on the next timer run without blocking the Sync API.

Only `.tex`, `.bib`, `.sty`, `.cls`, and `.bst` files are included.
Hidden paths and `build`, `generated`, `output`, `outputs`, `tmp`, and `__pycache__` directories are excluded.
PDFs, images, credentials/configuration files, and experiment outputs are not part of this repository.
Git retains reachable commits indefinitely and automatically packs objects to reduce storage.
Unchanged content advances the worker cursor without adding another history commit.
The history commit and cursor refs update atomically, so interrupted runs cannot acknowledge unsaved history.

## Inspect and recover

Run these commands on Fedora with access to the `thoughts` account.

```sh
sudo -u thoughts git --git-dir=/srv/thoughts/state/source-history.git log --oneline
sudo -u thoughts git --git-dir=/srv/thoughts/state/source-history.git log -p -- docs-course-notes/tex/source/paper_main.tex
sudo -u thoughts git --git-dir=/srv/thoughts/state/source-history.git show COMMIT:docs-course-notes/tex/source/paper_main.tex
sudo systemctl status thoughts-source-history.timer thoughts-source-history.service
sudo journalctl -u thoughts-source-history.service -n 20 --no-pager
```

Use recovered text through the normal authoring and Sync flow, preserving proposal requirements for agent edits.
Do not restore by checking files directly into the Fedora worktree; that directory is a reconstructable cache.
A future reader UI can expose this history without changing its storage format.

## Installation and backup

The native installer enables the timer.
An existing installation can use `sudo python3 deploy/fedora/install-source-history.py --apply` from a staged checkout containing the module, systemd units, and integration test.
This additive installer tests the change in the isolated Fedora test runtime, preserves divergent existing files, and installs the new module in both application and runtime trees.
It does not restart the API or existing workers.

The existing backup script includes the history repository when Restic is configured.
It holds the history lock while copying the repository so checkpoints and packing cannot race with the backup.
Installing source history does not configure a new remote backup destination.
A separate manual mirror must include all refs, for example with `git clone --mirror`, because `refs/thoughts/source-frontier` stores the export cursor.
Local Git history protects against unwanted edits but still requires an independent backup for disk loss.

## Validation

`tests/test_source_history.py` covers old-text recovery, deletion, unchanged content, interrupted atomic updates, object integrity, path filtering, multi-file commits, and mirror/repack recovery.
`tests/integration/source_history_fedora.py` runs with the isolated Fedora test runtime and a disposable database, worktree, object store, and history repository.
It checks baseline capture, Mac and Android publications, intermediate versions, empty runs, conflicts, database rollback, deletion, and cleanup without touching production or resetting the shared test API.
