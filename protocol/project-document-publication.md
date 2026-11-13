<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Publishing and unpublishing Project documents

Every document of a Project, built from LaTeX or Markdown or imported as a PDF, is either published or unpublished.
Unpublish is reversible: the document stays in its Project, its PDF, comments, review cards, schedules and feedback are kept, and Publish brings everything back as it was.
Remove (the existing Library-project action `remove_document`) is permanent: the document leaves the Project and its review cards are deleted (`review_event` with `rating: "delete"`, reason `document_deleted`), which nothing restores.

Status: server `project_document_publication_v1` (branch feature/document-publication). Desktop, Android and the Library connector implement §4–§6.

## 1. State

The published state is the existing `included` flag of the document's declaration in the Project manifest (schema 5, `thoughts-project.json`): `included: true` is published, `included: false` is unpublished.
No new manifest field is added. "Manage documents" check boxes and Publish/Unpublish edit the same flag, so they always agree.

Before this capability the flag only mattered for LaTeX and Markdown documents. Now it applies to imported PDFs (`format: "pdf"`) too.

## 2. Changing it

Clients send a Library-project action through `POST /v1/library/projects`, like `rename_document`:

```json
{"operation_id": "<uuid>", "project_id": "<project id>", "base_revision": 7,
 "action": "set_document_published", "document_id": "<document_id or catalogue id>", "published": false}
```

- `document_id` names the document by its permanent `document_id` or its catalogue `id`, published or not.
- `published` must be a boolean. Setting the current value is accepted and changes nothing but the revision.
- The server sets `included` in the manifest, saves it through the source revision log and bumps the Project revision.
- Nothing is deleted, retired or tombstoned.
- A client that edits the manifest directly (the "Manage documents" check boxes) still may; the server treats both the same.

`remove_document` and `rename_document` also accept an unpublished document.

## 3. Catalogue

`GET /v1/library/projects` keeps listing only published documents in each Project's `documents`, so older clients see unpublished documents as absent, exactly as before.

Each Project entry gains `unpublished_documents`: the unpublished documents in manifest order, with the same fields as `documents` and `"included": false, "published": false`.
Published documents carry `"published": true`.
Clients that know this capability list both, unpublished ones greyed.

An unpublished document is not a build target, is not listed in the Library (`/v1/library`, `/v2/library`, the source-layout index), and links to it become inactive.
An imported PDF that is also published in another Project stays listed.

## 4. Review

A review card belongs to an unpublished document when its document is unpublished in every Project that declares it.
The card's document is `primary.pdf_document_id` if present, else `primary.document_id`, else the `document_id` of the Library PDF `primary.pdf_stem + ".pdf"`.

Such a card is not offered for review, like a retired card (`review-card-retire.md` §5):
it is left out of library-wide and per-document review sessions, the next-card choice, due and overdue counts, "Skip all due" targets and Today summaries.
Its schedule, history, feedback and source markers are kept, and nothing is written to it.
Card lists may still show it, marked "Unpublished".
When the document is published again the card is offered again with its old schedule, unless it is retired or deleted.

The rule is derived; the server writes no per-card records.
Clients compute the set of unpublished document IDs from the catalogue (`unpublished_documents[].document_id` minus every `documents[].document_id`).

## 5. Clients

- Offer **Unpublish** and **Publish** on every Project document, and **Remove…** whose confirmation says the document's review cards are deleted permanently.
- Offer Unpublish/Publish only for a Project whose catalogue entry has `unpublished_documents` (present, possibly empty, exactly when the server has `project_document_publication_v1`), so the gate also works offline.
- Apply §4 wherever cards are chosen for review or counted as due, including agent tools (`thoughtsctl`, the desktop MCP and the Library connector).
- An unpublished document's PDF leaves the Library listing like an unchecked document's today; clients need not offer to open it. Publish lists it again.

## 6. Older clients

An older client sees an unpublished document as missing from its Project, as with an unchecked document today, and still offers its cards for review.
It cannot Publish it again, but a newer client or the "Manage documents" check box can.
