# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""move-pdf: move an imported PDF to another Library Project, as Thoughts' "Move to Project" does.

Before this an agent sent the Library-project action by hand (memory
thoughts-move-pdf-between-projects): POST /v1/library/projects
``{"action": "move_document", "operation_id", "project_id", "base_revision",
"document_id", "destination_project_id", "destination_revision"}``. The server moves
the PDF into the destination's default folder and its review cards with it; moving it
back undoes it (thoughts_server.document_moves.move_document).

Links need no edit: since 2026-11-10 \\PdfLink and \\CrossPdfLink find a Library PDF by
its filename wherever it is, so the result only lists the notes that link it.

This module checks the request and finds the PDF and the Projects in the catalogue;
client.py records the request, sends the action (again on a Project changed meanwhile,
with fresh revisions) and answers.
"""

from __future__ import annotations

from typing import Any

from . import documents
from .authoring import AuthoringRefused, _valid_uuid

MOVE_PDF = "move-pdf"
FIELDS = frozenset({"command", "pdf", "project", "request_id"})


def validate(request: Any) -> dict[str, str]:
    if not isinstance(request, dict) or request.get("command") != MOVE_PDF:
        raise AuthoringRefused("invalid_request", "The request must be a move-pdf JSON object.")
    for key in request:
        if key not in FIELDS:
            raise AuthoringRefused("invalid_request", f"Unexpected request field: {key}")
    for key in ("pdf", "project", "request_id"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise AuthoringRefused("invalid_request", f"Missing or invalid text field: {key}")
    if not _valid_uuid(request["request_id"]):
        raise AuthoringRefused("invalid_request", "request_id must be a lowercase UUID.")
    return {"pdf": request["pdf"].strip(), "project": request["project"].strip(), "request_id": request["request_id"]}


def find_pdf(catalogue: Any, wanted: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (Project, imported PDF) that wanted names: its document id, Library filename (with or without .pdf),
    or exact name."""
    entries = [(project, document) for project in (catalogue or {}).get("projects") or [] if isinstance(project, dict)
               for document in documents.documents(project) if document.get("imported")]
    filename = wanted if wanted.lower().endswith(".pdf") else wanted + ".pdf"
    for test in (lambda d: d.get("document_id") == wanted,
                 lambda d: filename in (d.get("filename"), d.get("attachment_filename")),
                 lambda d: d.get("name") == wanted):
        matches = [(project, document) for project, document in entries if test(document)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AuthoringRefused("ambiguous_document", f"Several imported PDFs match {wanted!r}; give its document "
                                                         "id.")
    raise AuthoringRefused("unknown_document", f"No imported PDF of the Library has the document id, filename or "
                                               f"name {wanted!r}. Only imported PDFs move; a note's PDF follows its "
                                               "source.")


def change(source: dict[str, Any], pdf: dict[str, Any], destination: dict[str, Any],
           operation_id: str) -> dict[str, Any]:
    return {"action": "move_document", "operation_id": operation_id, "project_id": source["id"],
            "base_revision": int(source.get("revision") or 0), "document_id": pdf["document_id"],
            "destination_project_id": destination["id"],
            "destination_revision": int(destination.get("revision") or 0)}


def holder(catalogue: Any, document_id: str) -> dict[str, Any] | None:
    """The Project that has the PDF now."""
    return next((project for project in (catalogue or {}).get("projects") or [] if isinstance(project, dict)
                 and any(document.get("document_id") == document_id for document in documents.documents(project))),
                None)
