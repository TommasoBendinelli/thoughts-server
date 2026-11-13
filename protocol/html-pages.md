<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# A docs folder's HTML pages

Status: version 1 (2026-11-09). Capability `html_pages_v1`. Server: `src/thoughts_server/html_pages.py`;
writer: `clients/thoughts-cli` (`thoughts_cli/html_pages.py`, `add-html-page`); Mac:
`thoughts-desktop tools/publisher/htmlpages.cpp`.

## 1. Model

An HTML page is one self-contained UTF-8 file of a connected `docs-*` folder:

```
docs-<folder>/tex/support/html/<name>.html      name: [A-Za-z0-9][A-Za-z0-9_.-]{0,122}\.html
```

A note of that folder links it with `\href{thoughts-html:<path>}{text}` (Markdown
`[text](thoughts-html:<path>)`), and Thoughts opens it in the sandboxed viewer (no network, forms,
frames or storage; `docs/HTML_ATTACHMENTS.md`). Only pages of the note's own folder resolve: a
PDF's source artifact carries the pages of its folder.

- **Bytes** are an object (`POST /v1/objects`, at most 100 MB, UTF-8). They are never a canonical
  source: no merges, no source history.
- **The folder's list** is the HTML attachment manifest `docs-<folder>/tex/support/thoughts_html_attachments.txt`
  (`{"schema_version": 1, "attachments": [{kind: "html_attachment", path, sha256, object, bytes}]}`). It is a
  canonical source, so every device receives it with the folder's other sources. Only the server writes it
  (`POST /v1/html-pages`, and the older Library action `import_html`); clients never publish it.
- **Builds.** Every PDF build of a folder's document materializes the listed pages and records them
  as `html_attachment` source resources, next to the HTML files a Mac uploads with the PDF's build
  inputs. The list wins: a listed page replaces a build input's copy of the same path, so a page
  changed on the server is in the next build even while a Mac still uploads its older copy. A page
  added or changed on the server is therefore in the next build of each of the folder's PDFs, and a
  client opens it from the PDF's source artifact.
- **Rebuilds.** A PDF of the folder is out of date (render state `out_of_sync`, "The folder's HTML
  pages changed after the latest server build.", selected by the next render check) when the list
  changed after its build, or when its recorded `html_attachment` resources miss a listed page or
  carry another version of one, although no compiler reads the list. A publication without recorded
  resources is not judged by its pages.
- A change of the list never changes a Library Project's revision, so it never makes another
  device's queued Project change stale.

## 2. API

`GET /v1/html-pages[?folder=docs-<folder>]`

```json
{"pages": [{"path": "docs-x/tex/support/html/a.html", "folder": "docs-x", "sha256": "…", "size_bytes": 1234}],
 "unlisted_pages": [{"path": "…", "folder": "docs-x", "sha256": "…", "size_bytes": 1, "targets": ["x.pdf"]}]}
```

Without `folder`, the pages of every folder (no `unlisted_pages`). With `folder`, also the pages that
only the folder's latest build-input snapshots carry (a Mac that has not listed them yet): they
exist, and a link to them works, but they are not in the list. `422 invalid_folder` for a name that
is not `docs-*`.

`POST /v1/html-pages`

```json
{"operation_id": "<uuid>", "folder": "docs-x",
 "changes": [{"path": "docs-x/tex/support/html/a.html", "sha256": "<new object or null>", "base_sha256": "<current or null>"}]}
```

1 to 100 changes, applied together in one new revision of the list, or none:

- `sha256` is the page's new object (uploaded first), `null` removes the page. `base_sha256` is the
  page's current object in the list, `null` when the list has none.
- A change whose page already has `sha256` is done and skipped (a retry). Any other `base_sha256`
  mismatch refuses the request: `409 html_page_changed` with `path` and `current_sha256`.
- `422 invalid_folder` (not a connected source folder of a Library Project), `422 invalid_html_page_path`
  (another path, or a name that differs from a listed one only in case), `409 html_page_object_unavailable`
  (object missing), `422 invalid_html_page` (empty, over 100 MB or not UTF-8), `422 invalid_request`.
- Answer: `{"folder", "changed": [paths], "pages": [the folder's pages]}`.

## 3. Writers

**thoughts-cli `add-html-page`** `{folder, name, html, replace?, request_id}` (the connector's
`add_html_page`): uploads the page and adds it, or with `replace: true` replaces the listed or unlisted
page of that name (`html_page_exists` otherwise). It answers `path`, `link`, a LaTeX and a Markdown
form, and a warning when the page loads resources from the network.

**Link check.** thoughtsctl on the Mac and thoughts-cli refuse a proposal whose replacement adds a
`thoughts-html:` link to a page its note's folder does not have: `invalid_link`, with `links[]` of
`{reason: html_page_not_found | html_page_other_folder, command: "thoughts-html", text, line, message,
suggestion?}`. Links already in the source are not checked.

## 4. The Mac

Each Sync's source steps (the source installer after Fedora's sources moved, and every source
publication) compare, for each folder, the local files `tex/support/html/*.html` (`L`), the folder's
pages on Fedora (`M`, `GET /v1/html-pages`) and the last version both agreed on (`S`, kept per path in
`html-pages.json` next to the publisher's state):

| L | M | S | Action |
|---|---|---|---|
| = M | | | agreed: `S := M` |
| none | some | `= M` | deleted here: remove it on Fedora (only while the folder's `tex/support/html` exists; otherwise install `M`) |
| none | some | other | install `M` |
| some | none | | new or changed here: add it on Fedora (`base null`) |
| some | some | `= L` | changed on Fedora: install `M` |
| some | some | `= M` | changed here: replace it on Fedora (`base M`) |
| some | some | other | both changed: keep `L` in `.thoughts/html-page-conflicts/<UTC time>/<path>`, install `M` |

All of one folder's uploads are one request. `html_page_changed` leaves `S` as it is, so the next Sync
decides again. A server without `html_pages_v1` (404) is skipped. Installing a page is an ordinary
local change of the folder: the Mac's next build inputs carry it too.
