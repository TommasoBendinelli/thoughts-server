<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# PDF link overrides

A PDF link override redirects one link of a Library PDF (and every other link of the same
PDF with the same destination) to another Library document. Example: in an imported paper,
the citation "(Pertsch et al., 2025)" jumps to the paper's own bibliography; the user picks
their Library copy of the cited paper, and from then on that citation opens it instead. The
choice syncs across devices.

## Record

Synced through the operations outbox (`POST /v1/operations`, pulled via `/v1/changes`,
`/v1/projections`, and `/v1/document-sync` of the PDF that contains the link):

- `entity_type`: `"pdf_link_override"`
- `collection`: `"link-overrides"`
- `entity_id`: a random canonical (lowercase) UUIDv4 created by the client, equal to `payload.override_id`
- conflict rule: last-writer-wins by priority (`revision`, then `updated_at`, `origin_id`,
  `operation_id`); `base_revision` is not checked. Delete is a tombstone (`deleted: true`)
  carrying the full record.
- server capability string: `"pdf_link_overrides_v1"` (advertised in `/v1/health`
  `capabilities`). Clients hold these operations until the server advertises it.

Payload:

```json
{
  "override_id": "uuid4 (= entity_id)",
  "source_document_id": "uuid4 of the PDF that contains the link",
  "source_pdf_sha256": "sha256 of that PDF when the override was made (informational)",
  "link": {
    "kind": "goto" | "uri",
    "dest_name": "cite.pertsch2025fast",      // goto, optional: named destination when the platform knows it
    "dest_page": 41,                          // goto: 0-based page index of the destination
    "dest_x": 72.0, "dest_y": 640.5,          // goto, optional: destination point in PDF user-space points
                                              //   (origin bottom-left of the page's MediaBox, like /XYZ)
    "uri": "https://arxiv.org/abs/2501.09747" // uri: the exact URI
  },
  "target_document_id": "uuid4 of the Library document to open",
  "revision": 1,
  "updated_at": "RFC3339",
  "origin_id": "device/origin id as used by other record types",
  "deleted": false
}
```

Server validation (HTTP 422 otherwise): `collection` is `link-overrides`; `override_id`
equals `entity_id`; `override_id`, `source_document_id` and `target_document_id` are canonical
UUIDv4; `source_pdf_sha256`, when present, is 64 lowercase hex digits; `revision` is an integer
≥ 1; `updated_at` and `origin_id` are non-empty; `deleted` is a boolean. `link.kind` is `goto`
or `uri`. A `goto` link requires `dest_page` (integer ≥ 0) and `dest_name` or `dest_y`
(coordinates are numbers). A `uri` link requires a non-empty `uri`.

Unknown payload fields (also inside `link`) are stored and returned unchanged; clients must
preserve them when they rewrite a record.

## Matching a clicked link (clients)

Among non-deleted overrides with `source_document_id` == the open document's id:

- URI link: `link.kind == "uri"` and `uri` equal (exact string).
- Internal GoTo link (same PDF, no file):
  - if both the override and the clicked link have `dest_name`, match iff names are equal;
  - otherwise match iff `dest_page` is equal AND, when the override has `dest_y`, the clicked
    link's destination y is within 4 pt (same for x when both have x). An override with
    neither name nor y never matches page-only; clients always store y when they know it.
- GoToR links and Thoughts' own links (thoughts-pdf-link, thoughts-html, `\PdfLink`…) are
  never overridable.
- Several matches: the one with the latest `updated_at` wins.

If the target document is not in the local Library, clients show a short message and follow
the original link.

## Document deletion

When a document is deleted (Library removal, Project or document removal, or deletion of the
document's entry source), the server tombstones every override whose `source_document_id` is
that document, with `deletion_reason: "document_deleted"` and the `document_deletion`
evidence used by other cascaded records. A document whose identity still names a live source
(a moved document keeps its ID) is not treated as deleted. Overrides whose
`target_document_id` is the deleted document are kept; clients fall back to the original link.

A later non-deleted write to a cascade-tombstoned override, while its document stays deleted,
is answered with outcome `superseded` (not a conflict), so it does not block the client's
other queued operations; the client receives the tombstone through `/v1/changes`.
