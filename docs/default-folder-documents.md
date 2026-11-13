<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Documents in a Project's default source folder

A Project's configuration lives at
`.thoughts/projects/<sha256(project id)>/default/thoughts-project.json`. That
directory is the Project's *default source folder*. Libraries created before
2026-10-17 call it `documents`; see [Folder name](#folder-name-default-legacy-documents). Besides the configuration and
imported PDFs it may hold Markdown (`.md`, `.markdown`) and standalone LaTeX
documents, declared with `source.folder_id = default_folder_id`. They register,
build, publish, appear in the Library and carry comments, review cards, source
proposals and source links exactly like documents of a connected `docs-*` folder.

The same rules are implemented by the desktop app, its publisher and Android.

## Source owner

`thoughts_server.source_owner` is the only place that derives the folder owning
a docs-root-relative path:

- `docs-<name>` when the first segment matches `docs-[A-Za-z0-9_.-]+`;
- `.thoughts/projects/<h>/default` (or the legacy `.thoughts/projects/<h>/documents`)
  when the path starts with exactly these four segments and `<h>` is 64
  lowercase hex digits;
- otherwise no owner.

Owners are carried as these full paths everywhere: build snapshots
(`BuildInputSnapshot.project`), publications and source artifacts (`pdf.project`),
the source index, closure expectations, renderer `ProjectBuild`/`SourceBuild`
keys, source ids (`<owner>/<relative path>`) and link selectors. A folder on disk
is resolved with `docs_root / owner`; `locate(folder)` recovers the owner of an
absolute folder from its last four segments. Never use a directory's base name
(every default folder has the same one) or the first segment alone (every
default folder starts with `.thoughts`), and never assume a fixed owner length
(`default` gives 91 characters, `documents` 93).

The Project configuration and imported PDFs keep their historical handling:
they are inputs of every folder's documents, not sources of the default folder.

## Output filename

A latex or markdown document in the default folder publishes as
`document_` + the first 20 hex digits of SHA-256 over `<owner>/<relative path>` +
`.pdf`, with the owner always written in its legacy spelling
`.thoughts/projects/<h>/documents`, so PDF names did not change with the folder
rename. An explicit `rendering[document_id].output` still wins; clients write
this derived name there when they declare the document. `docs-*` names are
unchanged. The default folder never has a `<short>_combined.pdf` aggregate, and
its folder identity may predate random UUIDs.

## Compilation

A default-folder LaTeX document compiles from its own directory unless a working
directory is declared. Its build and PDF outputs are written to `build/` and
`pdf/` inside the default folder, as for `docs-*` folders.

## Ignore rules

The default folder honours its own `.ThoughtIgnore`. Without one, its built-in
rules are `.git/`, `.DS_Store`, `build/` and `pdf/`, so build leftovers never
appear in Sources or Sync. `docs-*` folders keep the single `.git/` default.

## Library

The catalogue attaches publications whose project is the default folder to the
Project and lists the folder's authored files in its default-source entry.
`include_source` declares a Markdown file or a standalone LaTeX file
(`\documentclass` and `\begin{document}` outside comments) of the Project's own
default folder by its real relative path.

## Out of scope

These stay `docs-*` only and are skipped or refused for the default folder:
the shared `tex/support/manual_style*.sty` project style (style lookups return
none), HTML attachments, legacy pre-v5 settings files, and moving or removing
the default folder. Frozen project-scoped publication bundles are a client
feature and stay `docs-*` only there.

## Folder name: `default` (legacy `documents`)

The default folder used to be called `documents`. The server migration renames
it to `default` in every Library at once; clients follow on their next Sync.

**Rule R** is the only rewrite: every match of
`\.thoughts/projects/([0-9a-f]{64})/documents(?=/|"|'|\\|$|\s)` becomes
`.thoughts/projects/\1/default`, applied textually (also inside absolute and
`../` paths). `thoughts_server.default_folder_segment` implements it
(`rewrite_legacy_default_folder`), as do the desktop app, its publisher and
Android.

Compatibility (all codebases, before the migration):

- every parser, glob, `LIKE`, validator and owner check accepts both spellings;
- an existing Project's folder is the one its manifest declares
  (`source_folders[default_folder_id].path`); on disk `default/` wins over
  `documents/`; both at once for one Project is an error, never merged;
- the manifest's default folder must be `.thoughts/projects/<sha256(id)>/default`
  or `.../documents`, and the manifest must live inside that folder;
- new default folders use the Library's segment: the server's
  `server_settings.default_folder_segment` (absent = `documents`), which every
  server process reads when it opens the database.

### Server migration

`python -m thoughts_server.migrate_default_folder --root <worktree> [--canonical]`
previews; `--canonical --apply` migrates the repository server with every service
stopped; `--canonical --rollback <backup>` restores. Apply refuses unresolved conflicts
under a legacy folder, a Project with both spellings, and worktree drift; it backs
up the database and every legacy folder to
`/srv/thoughts/backups/default-folder-<UTC>-<hex8>/` (`inventory.json` lists
paths and SHA-256) and then, in one transaction:

- moves each live source to its Rule-R path with the same revision and bytes
  (operation payload `outcome: "renamed"`, `renamed_from`), tombstoning the old
  path (payload `renamed_to`);
- saves each manifest's Rule-R bytes at the new path as revision + 1;
- records `default_folder_segment = default`;
- applies Rule R in place to every other path-bearing column (entities and
  non-source operations, document identities, publications, build-input
  snapshots, resolved conflicts, agent notes, traces, ...); source operation
  history and source tombstones keep their legacy paths;
- queues a forced rebuild of the whole Library.

It then renames the folders on disk, writes the new manifests, rewrites the
worktree's `comments/` exports, and fails unless a final scan finds no legacy
path left outside source history. A second run is a no-op.

Until the rebuild finishes, published artifacts still name `documents`; the
server compares owners in either spelling and shows their segments under the
new name.

### Alias

After the migration every JSON request body, URL path and query string passes
through Rule R (`thoughts_server.default_folder_alias`), so a client that has not
migrated yet, or a request queued before the migration, still addresses the
renamed sources. Retries of operations, publications and Library changes made
before the migration are recognised by their original (legacy) identity.
