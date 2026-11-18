# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""create-project, add-source-folder and move-document: Projects with a docs folder of their own, from any device.

A Project made with "create" has only its hidden default folder (.thoughts/projects/<hex>/default):
its notes cannot have interactive HTML pages or agent figures, and cannot move to another Project.
The Mac adds a connected docs-* folder with Import folder. A device without one asks the server
(capability library_project_source_folder_v1): create-project makes the Project with its own folder
(the default; source_folder false makes it without), add-source-folder gives an existing Project
one more. The folder is docs-<label slug>-<hex>, listed under its label (the Project's name unless
folder_name/name says otherwise), and the Mac and phone show it after their next Sync.

move-document moves a LaTeX or Markdown note of a docs-* folder into another Project, as the Mac's
"Move to project" does: the server plans it (plan_move_source_document: which files move or are
copied, the links to rewrite, the pages to list) and the move carries the plan's digest
(move_source_document), so a plan that changed meanwhile is planned again rather than applied. The
note keeps its identity and PDF, its review cards and comments follow, links to and from it are
rewritten, and its interactive pages are listed in the new folder. A Project without a docs folder
gets one with the move. A plan that needs files only the Mac holds (transfer "desktop") is refused:
then the user moves it on the Mac. dry_run answers the plan without moving.
"""

from __future__ import annotations

from typing import Any

from . import documents
from .authoring import AuthoringRefused, _valid_uuid

CREATE_PROJECT = "create-project"
ADD_SOURCE_FOLDER = "add-source-folder"
MOVE_DOCUMENT = "move-document"
COMMANDS = (CREATE_PROJECT, ADD_SOURCE_FOLDER, MOVE_DOCUMENT)
CAPABILITY = "library_project_source_folder_v1"
# (required, optional) fields of each command besides "command".
FIELDS = {
    CREATE_PROJECT: ({"name", "request_id"}, {"source_folder", "folder_name"}),
    ADD_SOURCE_FOLDER: ({"project", "request_id"}, {"name"}),
    MOVE_DOCUMENT: ({"document", "project", "request_id"}, {"destination_path", "dry_run"}),
}
LABELS = ("name", "folder_name")


def invalid(message: str) -> AuthoringRefused:
    return AuthoringRefused("invalid_request", message)


def validate(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("command") not in COMMANDS:
        raise invalid("The request must be a create-project, add-source-folder or move-document JSON object.")
    command = request["command"]
    required, optional = FIELDS[command]
    for key in request:
        if key != "command" and key not in required | optional:
            raise invalid(f"Unexpected request field: {key}")
    for key in required:
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise invalid(f"Missing or invalid text field: {key}")
    if not _valid_uuid(request["request_id"]):
        raise invalid("request_id must be a lowercase UUID.")
    result = {key: value.strip() if isinstance(value, str) and key != "request_id" else value
              for key, value in request.items() if key != "command"}
    for key in LABELS:
        value = result.get(key)
        if key in result and (not isinstance(value, str) or not value or len(value) > 200
                              or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise invalid(f"{key} must have 1 to 200 characters without line breaks.")
    for key in ("source_folder", "dry_run"):
        if key in result and not isinstance(result[key], bool):
            raise invalid(f"{key} must be true or false.")
    if "destination_path" in result and (not isinstance(result["destination_path"], str)
                                         or not result["destination_path"]
                                         or result["destination_path"].startswith("/")):
        raise invalid("destination_path must be a relative path inside the destination folder.")
    if command == CREATE_PROJECT and result.get("source_folder") is False and "folder_name" in result:
        raise invalid("folder_name names the Project's own folder; leave it out with source_folder false.")
    return result


def supported(api) -> bool:
    return CAPABILITY in (api.health().get("capabilities") or [])


def unsupported(what: str) -> AuthoringRefused:
    return AuthoringRefused("server_unsupported", f"The server cannot {what} yet ({CAPABILITY}); nothing was "
                                                  "changed.")


def connected_folders(project: dict[str, Any] | None) -> list[dict[str, str]]:
    """The Project's docs-* folders with their labels: [{folder, name}]."""
    configuration = (project or {}).get("project_configuration") or {}
    default = configuration.get("default_folder_id")
    paths = [f.get("path") for f in configuration.get("source_folders") or []
             if isinstance(f, dict) and f.get("folder_id") != default and isinstance(f.get("path"), str)]
    labels = {f.get("project"): f.get("folder") for f in (project or {}).get("source_folders") or []
              if isinstance(f, dict)}
    return [{"folder": path, "name": labels.get(path) or path} for path in paths]


def find_note(catalogue: Any, wanted: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (Project, note) wanted names: its document id, source path, or exact name. Imported PDFs are refused."""
    entries = [(project, document) for project in (catalogue or {}).get("projects") or [] if isinstance(project, dict)
               for document in documents.documents(project)]
    for test in (lambda d: d.get("document_id") == wanted, lambda d: d.get("source_file") == wanted,
                 lambda d: d.get("name") == wanted):
        matches = [(project, document) for project, document in entries if test(document)]
        if len(matches) > 1:
            raise AuthoringRefused("ambiguous_document", f"Several Library documents match {wanted!r}; give its "
                                                         "document id.")
        if matches:
            project, document = matches[0]
            if document.get("imported") or not isinstance(document.get("source_file"), str):
                raise AuthoringRefused("not_a_note", f"{wanted!r} is an imported PDF; move it with move-pdf.")
            return project, document
    raise AuthoringRefused("unknown_document", f"No Library note has the document id, source path or name {wanted!r}.")


def plan_change(origin: dict[str, Any], document_id: str, destination: dict[str, Any],
                destination_path: str | None) -> dict[str, Any]:
    change = {"action": "plan_move_source_document", "project_id": origin["id"], "document_id": document_id,
              "destination_project_id": destination["id"], "carry_preamble": True}
    if destination_path:
        change["destination_path"] = destination_path
    return change


def move_change(plan: dict[str, Any], operation_id: str) -> dict[str, Any]:
    """The operation that applies a reviewed plan, with the inputs the server plans it from again."""
    return {"action": "move_source_document", "operation_id": operation_id,
            "project_id": plan["source"]["project_id"], "base_revision": int(plan["source"]["revision"]),
            "document_id": plan["document_id"], "destination_project_id": plan["destination"]["project_id"],
            "destination_revision": int(plan["destination"]["revision"]),
            "destination_path": plan["destination"]["path"], "carry_preamble": True,
            "plan_digest": plan["plan_digest"]}


def check_plan(plan: dict[str, Any]) -> None:
    """Refuse what only the user can decide, or only the Mac can do."""
    destination = plan["destination"]
    if destination.get("collision"):
        raise AuthoringRefused(
            "destination_exists", f"{destination['folder']}/{destination['path']} already exists; give "
            f"destination_path, e.g. {destination.get('suggested_path')!r}.", suggested_path=destination.get("suggested_path"))
    desktop = sorted(f["source"] for f in plan.get("files") or [] if f.get("transfer") == "desktop")
    if desktop:
        raise AuthoringRefused(
            "needs_mac", "This move needs files only the Mac holds (" + ", ".join(desktop) + "); nothing was "
            "moved. Ask the user to move it on the Mac with Move to project.", files=desktop)


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    destination = plan["destination"]
    return {
        "document_id": plan["document_id"], "name": plan.get("name"),
        "from_project": plan["source"]["project_name"], "to_project": destination["project_name"],
        "from": plan["source"]["folder"] + "/" + plan["source"]["path"],
        "to": destination["folder"] + "/" + destination["path"],
        "creates_folder": bool(destination.get("new_folder")),
        "files": [{key: f.get(key) for key in ("source", "destination", "action", "transfer", "reason")}
                  for f in plan.get("files") or []],
        "edited_sources": sorted(plan.get("link_edits") or {}),
        "html_pages": [page["destination"] for page in plan.get("html_pages") or []],
        "review_card_markers": (plan.get("markers") or {}).get("review_cards", 0),
        "warnings": list(plan.get("warnings") or []),
    }
