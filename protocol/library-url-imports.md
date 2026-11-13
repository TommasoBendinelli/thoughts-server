<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Importing a PDF from a URL (capability `library_import_pdf_v1`)

A client that has no PDF file to upload, such as a remote agent connector, asks the server to download
the PDF and import it into a Library Project:

```
POST /v1/library/url-imports
{"project": "<Project id, or its name ignoring case>", "name": "<document name>", "url": "https://..."}
```

The server makes exactly the changes the desktop's Import document makes for a link import:

1. It downloads the URL (rules below) and stores the PDF in the object store by SHA-256.
2. The PDF becomes a standalone Library PDF, as `POST /v1/library/imports` makes it: a new permanent
   document identity, `current_pdfs/<filename>`, a publication of kind `unlinked`. The filename is the
   desktop's: the name with every character outside `A-Za-z0-9_.-` replaced by `_` (prefixed with
   `document_` when it does not start with a letter or digit), then `_`, the first 12 hex digits of the
   PDF's SHA-256, `_`, the first 8 hex digits of the SHA-256 of the Project id, and `.pdf`.
3. It applies the Library-project change `import` (`POST /v1/library/projects`) with that filename,
   SHA-256 and name, so the PDF is declared in the Project's configuration and copied into its default
   folder.

The URL is kept as the document's source link in the publication's metadata, `metadata.source_url` of
its `/v1/library` entry. Other devices see the new document after their next Sync, as for any import.

A PDF the Project already has (a document with the same SHA-256) changes nothing: the answer has
`replayed: true` and names the existing document. Retrying a call is therefore safe.

## Download rules

- `https` only, on port 443, without a user name or password in the URL.
- Every address the host resolves to must be public: loopback, private, link-local, shared
  (100.64.0.0/10), multicast, reserved and unspecified addresses are refused, also in their IPv6
  spellings (IPv4-mapped, 6to4, NAT64, Teredo). The connection goes to the address that was checked.
- Redirects (301, 302, 303, 307, 308) are followed up to 5 times; each one is checked by the same rules.
- The download has a 120 s deadline (30 s per network wait) and a 100 MiB cap.
- The body must start with `%PDF-` and be readable as a PDF with at least one page.

## Answer

`200`:

```json
{"accepted": true, "replayed": false, "project_id": "...", "project": "Reading",
 "document_id": "...", "filename": "A_paper_0123456789ab_89abcdef.pdf", "name": "A paper",
 "sha256": "...", "size_bytes": 123456, "page_count": 12, "source_url": "https://..."}
```

A refusal changes nothing and answers `{"accepted": false, "error_code": ..., "detail": ...}`:

| Status | `error_code` | Meaning |
|---|---|---|
| 404 | `project_not_found` | No Project has that id or name; `detail` lists the Projects. Checked before downloading. |
| 409 | `project_ambiguous` | Several Projects have that name; give the id. |
| 409 | `filename_taken` | A different Library PDF already uses the filename. |
| 409 | `import_refused` | The Project change was refused (for example the Project changed meanwhile); retry. |
| 413 | `too_large` | Over 100 MiB. |
| 422 | `invalid_url`, `invalid_name` | The request is not valid (name: 1 to 150 characters, no line breaks). |
| 422 | `address_refused` | The host, or a redirect, resolves to a non-public address. |
| 422 | `not_pdf`, `invalid_pdf` | The URL did not return a PDF (for arXiv use the `/pdf/` link), or one that cannot be read. |
| 502 | `download_failed` | Host not found, HTTP error, too many redirects, or the deadline passed. |
