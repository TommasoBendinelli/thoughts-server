<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Global incremental sync and editable-source acknowledgement

The visible Sync action remains global. Startup performs no network synchronization. Cached PDFs, observed server state, and installed editable sources have separate checkpoints. An empty upload queue cannot establish that remote source edits were installed.

## Decisions and installation

`protocol/sync_rules.json` is the versioned decision policy. `scripts/generate_sync_core.py` generates the C++ and Kotlin implementations; `--check` detects drift. Every implementation runs `protocol/fixtures/sync-policy.json`. (The generated Python implementation was removed together with the desktop Python client; see the note below.) Adapters supply explicit facts, including whether a missing file means an authored deletion or an evicted Android cache.

Desktop checks its materialized-source frontier against freshly observed Fedora state before taking the unchanged fast path. The desktop source reconciler reconciles canonical heads with the actual editable files and their last common bases. It verifies object hashes, rejects unsafe paths, checks for edits during downloads, installs atomically, then writes per-file receipts. Downloads may use four independent workers and already verified cached objects. A cached download alone never advances an editable-source receipt. Shared source publication and installation use the same interprocess state lock.

A missing merge base stays unresolved; it is never invented from a newer PDF. A conflict blocks its source path and affected publication while healthy paths continue. Resolution must be explicitly recorded by Fedora, and only the local generation that entered that conflict can be replaced automatically. Later edits remain separate pending work. Existing first-time multi-file publication remains an atomic operation: a newly discovered conflict can still require a retry before the affected target is isolated.

Android uses the same source decisions, checks installation before acknowledging its working-copy base, and retains per-path receipts plus a small attention marker. Independent valid sources can install even when another path needs attention. Final success checks the attention marker.

## Avoiding repeated work

Independent Overleaf Git fetches overlap with each other and a fresh Fedora source-tree read. Local reconciliation remains serialized because bridges share publication state. A successful bridge checkpoint fingerprints both actual managed source bytes and canonical project heads. Only an unchanged, synchronized checkpoint and unchanged Overleaf Git state skip full reconciliation. Missing checkpoints, additions, deletions, local edits, and remote edits take the regular path. The surrounding global sync still checks other build inputs and publications.

Desktop retains verified physical file identities when accepting source receipts, avoiding another external hash process for every unchanged file. Source and source-index cache receipts now persist optional empty fields as empty strings rather than SQL nulls. This fixes the silent database constraint failure that prevented the real app from establishing its reader-cache checkpoint.

## Evidence and debugging

A normal sync has one durable run ID in `sync-operations.sqlite`. Source decisions record their action and reason. Full installer reports are saved under `source-reconciliation-reports/`, keyed by trace and attempt time; the journal retains a compact summary and the report path. Overleaf completion events retain per-project timing, outcome, and checkpoint reason. A refused reader checkpoint records the missing object identity.

The `thoughts_server.sync_explain` command that read this local evidence was removed with the desktop Python client. Its report had a `timeline` containing non-overlapping Desktop coordinator intervals; `bottlenecks` sorts the five largest intervals. Worker timings remain separate because they may overlap or nest. Distinguish time waiting for a server build from time compiling, source installation from downloading, and terminal failure from a transport failure in the command client.

In the measurements of this redesign, the unchanged global observation fell from 18.312 seconds before the final fixes to 4.033 seconds after the provider and cache fixes; the final installed build completed another unchanged global sync in 3.432 seconds. Both configured Overleaf projects remained included. These are individual measurements, not p50/p95 claims. A separate run with pending source edits and real PDF builds took 99.989 seconds and must not be compared with an unchanged run.

The shared rules, adapters, and diagnostics are deployed locally; this is not a replacement of every platform coordinator with one executable. The existing renderer caches are retained. Android was installed with its private data preserved. After reconnecting the phone, a global sync completed successfully with no source-installation problems. The on-device hash of an editable source matched Desktop's, and its receipt recorded a verified remote-only installation. No phone latency claim is made from this check.

## Regression coverage and next measurements

The tests cover remote-only edits with an empty upload queue, evicted caches, missing lineage, both writers changing a file, edits during transfer, corrupt objects, interrupted acknowledgement, independent healthy paths, resolved versus unresolved conflicts, and identical policy outcomes across languages. Desktop additionally tests the no-op state endpoint and cache persistence with omitted optional fields. Provider tests invalidate unchanged checkpoints after a Fedora-only edit.

For subsequent performance changes, retain the run ID and classify the scenario: unchanged, logical-only, one source edit, remote-only source edit, changed PDF, offline retry, or conflict. Compare the same scope and device state. Use the durable intervals to choose the next change; run fault and restart regressions before changing checkpoint behavior. Render-input invalidation and actual rebuild latency remain separate optimization work from the unchanged sync path.

## Note: desktop Python client removed (2026-10-05)

Desktops now publish sources with the native C++ publisher bundled in the Thoughts desktop app. The Python client modules referenced by this record (`source_agent`, `source_reconciliation`, `review_source_receipts`, `source_lock`, `build_watch`, `overleaf_bridge`, `sync_core`, `sync_explain`) and the `thoughts-source-sync`, `thoughts-publish-sources`, and `thoughts-watch-build` commands were deleted from this repository. The server keeps only its authored-source inventory (`source_inventory`) and the provider Git operations (`provider_git`).
