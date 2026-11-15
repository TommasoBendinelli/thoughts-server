# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Move one source-linked document into another Project's source folder.

The document keeps its permanent identity and its PDF filename (pinned through
``rendering[document_id].output``), so its publication, PDF annotations, review
cards, history and every client cache keyed by the filename stay attached.
Only what is keyed by the source path changes: the source file itself moves,
review-card source anchors, comment scopes and legacy annotation paths are
rewritten to the new path, and both Project manifests change, all in one
database transaction recorded with a ``move_pending`` receipt (as
``document_moves`` does) so a crash before the worktree is materialized is
repaired on the next Library operation.

Files are of two kinds. Authored sources (``.tex``, ``.sty``, ``.bib``…) are
canonical on Fedora and moved or copied here. Build inputs (figures, generated
fragments) are published by the desktop that holds them; the plan lists them
as ``transfer: desktop`` and that desktop copies them before sending the
operation and removes moved originals after Fedora accepts it.

``plan`` is read-only: the desktop shows it, and the operation carries the
plan's structural digest so the server refuses a move whose plan changed meanwhile
(new users of a file, a different preamble) instead of applying something the
user did not review. Content changes alone don't invalidate a plan: the latest
canonical text is what moves.

A Project created without a source folder has only its hidden default folder.
A move into it creates the Project's first connected folder, in the same
transaction: ``plan`` names it (``destination.new_folder``), the way an
imported folder is named (Rule N: ``docs-<Project name slug>-<hex>``, the hex
from the destination and the document, so the plan and the operation name the
same folder), and the analysis gives it what the document needs to build there
(the origin's project style and bibliography). The digest covers the new
folder's name, so a plan reviewed before the Project got a folder elsewhere is
refused rather than applied into another folder.
"""
from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select, update

from .models import (AgentProposalResolution, BuildInputSnapshot, Device, DocumentIdentity, Entity, Publication,
                     ReviewReanchorJob, SourceFile, utc_now)
from . import source_move_analysis as analysis

DEVICE = "thoughts-document-moves"
PLAN_ACTION = "plan_move_source_document"
ACTION = "move_source_document"
TEXT_SUFFIXES = (".tex", ".md", ".markdown")


def _configuration_folder(configuration: dict, folder_id: str) -> dict | None:
    return next((f for f in configuration["source_folders"] if f["folder_id"] == folder_id), None)


def _connected_folders(configuration: dict) -> list[dict]:
    return [f for f in configuration["source_folders"] if f["folder_id"] != configuration["default_folder_id"]]


def _build_input_entries(session, owner: str) -> dict[str, dict]:
    """Latest published asset/generated inputs of a folder: path -> entry."""
    from .build_inputs import latest_build_input_identities
    result: dict[str, dict] = {}
    identities = [i for i in latest_build_input_identities(session).values() if i.get("project") == owner]
    for identity in identities:
        snapshot = session.get(BuildInputSnapshot, identity["sha256"])
        if snapshot is None:
            continue
        for entry in snapshot.entries_json or []:
            path = entry.get("path", "")
            if entry.get("role") in {"asset", "generated"} and path.startswith(owner + "/"):
                result.setdefault(path, entry)
    return result


class _CanonicalFolder(analysis.Folder):
    """A folder's canonical sources plus the build inputs its desktop published."""

    def __init__(self, session, store, owner: str):
        sources = {row.path: row for row in session.scalars(
            select(SourceFile).where(SourceFile.deleted.is_(False), SourceFile.path.like(owner + "/%")))}
        inputs = _build_input_entries(session, owner)
        self.kinds = {}
        digests = {}
        for path, row in sources.items():
            self.kinds[path[len(owner) + 1:]] = "source"
            digests[path[len(owner) + 1:]] = (row.object_sha256, row.sha256)
        for path, entry in inputs.items():
            relative = path[len(owner) + 1:]
            if relative not in self.kinds:
                self.kinds[relative] = "build_input"
                digests[relative] = (entry["sha256"], entry["sha256"])
        self.digests = digests

        def read(relative):
            obj, _ = digests[relative]
            location = store.path(obj)
            return location.read_bytes() if location.is_file() else None

        super().__init__(owner, digests.keys(), read, lambda relative: digests[relative][1])


def _package_source(name: str) -> str | None:
    """A TeX package's source, when this host has a TeX installation."""
    import shutil
    import subprocess
    if not shutil.which("kpsewhich") or not name.replace("-", "").replace("_", "").isalnum():
        return None
    try:
        located = subprocess.run(["kpsewhich", name + ".sty"], capture_output=True, text=True, timeout=10).stdout.strip()
        if located:
            with open(located, encoding="utf-8", errors="replace") as handle:
                return handle.read()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _new_folder_name(session, store, root, configuration: dict, document_id: str) -> str:
    """The first connected folder a move creates in ``configuration``'s Project.

    Named as an imported folder is (Rule N, source_folder_names.import_folder_name):
    the Project's name for the slug, and for the hex a UUID of the destination
    and the document instead of an operation id, since the plan has none; a
    retried operation therefore names the same folder. Unique case-insensitively
    against every folder name in use or used before, and every Project's folders.
    """
    from .project_configuration import canonical_projects
    from .source_folder_names import import_folder_name, taken_names
    taken = taken_names(session, root) | {folder["path"] for value in canonical_projects(session, store)
                                          for folder in value["source_folders"]}
    seed = uuid.uuid5(uuid.NAMESPACE_URL, f"thoughts:moved-document-folder:{configuration['id']}:{document_id}")
    return import_folder_name(configuration["name"], str(seed), taken)


def _new_folder_declaration(folder: str) -> dict:
    """The new folder's (empty) source-folder manifest, as an import declares one.

    Its permanent aggregate identity follows from the folder's unique name, and
    project_configuration.add_folder derives the folder id from it as for an import.
    """
    digest = hashlib.sha256(("thoughts:moved-document-folder:" + folder).encode()).digest()
    return {"schema_version": 4, "document_id": str(uuid.UUID(bytes=digest[:16], version=4)), "documents": []}


def _structural_digest(plan: dict) -> str:
    shape = {
        "document": plan["source"]["path"], "destination": plan["destination"]["path"],
        "files": sorted((f["source"], f["destination"], f["action"], f["transfer"]) for f in plan["files"]),
        "carried": plan["preamble"]["carried"],
        "bibliography": sorted(e["key"] for e in plan["bibliography"]["missing"]),
        "edited": sorted(plan["link_edits"]),
    }
    if plan["destination"].get("new_folder"):
        # Only a new folder adds to the shape: digests of every other move stay as they were.
        shape["new_folder"] = plan["destination"]["folder"]
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()


def plan(session, store, root, request: dict) -> dict:
    """Describe exactly what ``move_source_document`` would do now (read-only)."""
    from .library_projects import TYPE, _record, catalogue
    from .project_configuration import canonical_for_project
    origin_id = request.get("project_id")
    destination_id = request.get("destination_project_id")
    document_id = request.get("document_id")
    if not isinstance(destination_id, str) or destination_id == origin_id:
        raise ValueError("Choose a different destination project")
    projects = {p["id"]: p for p in catalogue(session, root, store)["projects"]}
    if origin_id not in projects:
        raise ValueError("This project is no longer in the Library")
    if destination_id not in projects:
        raise ValueError("The destination project is no longer available")
    origin_configuration = canonical_for_project(session, store, origin_id)
    destination_configuration = canonical_for_project(session, store, destination_id)
    if origin_configuration is None or destination_configuration is None:
        raise ValueError("Both Projects require current authoritative manifests before moving a document")
    declaration = next((d for d in origin_configuration["documents"]
                        if d["document_id"] == document_id
                        or analysis_alias(origin_configuration, d) == document_id), None)
    if declaration is None:
        raise ValueError("The document is no longer in this project")
    if declaration["format"] not in {"latex", "markdown"}:
        raise ValueError("Only LaTeX and Markdown source documents move with their source; use Move PDF for PDFs")
    document_id = declaration["document_id"]
    origin_folder = _configuration_folder(origin_configuration, declaration["source"]["folder_id"])
    if origin_folder is None or origin_folder["folder_id"] == origin_configuration["default_folder_id"]:
        raise ValueError("Documents of a Project's own default folder cannot move yet; move a connected source folder's document")
    folders = _connected_folders(destination_configuration)
    wanted_folder = request.get("destination_folder")
    new_folder = None
    if wanted_folder:
        folders = [f for f in folders if f["path"] == wanted_folder]
    elif not folders:
        # Only its hidden default folder: the move gives the Project its first source folder.
        new_folder = _new_folder_name(session, store, root, destination_configuration, document_id)
    if not folders and new_folder is None:
        raise ValueError("The destination project has no source folder. Move or import a source folder into it first")
    destination_folder = folders[0] if folders else {"folder_id": None, "path": new_folder}
    origin_owner, destination_owner = origin_folder["path"], destination_folder["path"]
    old_relative = declaration["source"]["path"]
    origin = _CanonicalFolder(session, store, origin_owner)
    destination = _CanonicalFolder(session, store, destination_owner)
    if old_relative not in origin.paths or origin.kinds.get(old_relative) != "source":
        raise ValueError("The document's source is not on the server yet. Sync and try again")

    wanted = request.get("destination_path") or old_relative
    candidate = PurePosixPath(wanted)
    if (candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != wanted
            or candidate.suffix.lower() != PurePosixPath(old_relative).suffix.lower()
            or any(part.startswith(".") for part in candidate.parts)):
        raise ValueError("Choose a file name with the document's extension inside the destination folder")
    new_relative = wanted
    taken = set(destination.paths) | {d["source"]["path"] for d in destination_configuration["documents"]
                                      if d["source"]["folder_id"] == destination_folder["folder_id"]}
    collision = new_relative in taken or session.scalar(select(DocumentIdentity).where(
        DocumentIdentity.source_path == destination_owner + "/" + new_relative,
        DocumentIdentity.document_id != document_id)) is not None
    suggestion = analysis.free_name(taken, new_relative) if collision else None

    other_documents = [d["source"]["path"] for d in origin_configuration["documents"]
                       if d["source"]["folder_id"] == origin_folder["folder_id"] and d["document_id"] != document_id]
    destination_documents = [d["source"]["path"] for d in destination_configuration["documents"]
                             if d["source"]["folder_id"] == destination_folder["folder_id"]]
    result = analysis.analyse(origin, destination, old_relative, new_relative,
                              other_documents=other_documents, destination_documents=destination_documents,
                              package_source=_package_source, document_format=declaration["format"],
                              new_folder=new_folder is not None)

    if request.get("carry_preamble") is False and result["preamble"]["carried"]:
        result["preamble"]["carried"] = ""
        result["preamble"]["declined"] = True
    files = []
    for item in result["files"]:
        kind = origin.kinds.get(item["source"], "source")
        item = {**item, "transfer": "fedora" if kind == "source" else "desktop"}
        files.append(item)

    # Links in other documents that name the moved document.
    inbound = {}
    old_name = PurePosixPath(old_relative).name
    for row in session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False))):
        if not row.path.lower().endswith(TEXT_SUFFIXES) or row.path == origin_owner + "/" + old_relative:
            continue
        if row.path.startswith(origin_owner + "/") and row.path[len(origin_owner) + 1:] in {f["source"] for f in files if f["action"] in {"move", "present-move"}}:
            continue
        raw = store.path(row.object_sha256).read_bytes()
        if old_name.encode() not in raw:
            continue
        from .source_owner import owner_of
        owner = owner_of(row.path)
        text = raw.decode("utf-8", errors="replace")
        if row.path.lower().endswith((".md", ".markdown")):
            edits = analysis.markdown_inbound_link_edits(text, row.path, origin_owner, destination_owner,
                                                         (old_relative, new_relative))
        else:
            edits = analysis.inbound_link_edits(text, owner or "", origin_owner, destination_owner,
                                                (old_relative, new_relative))
        if edits:
            inbound[row.path] = [{"start": e.start, "end": e.end, "replacement": e.replacement, "reason": e.reason}
                                 for e in edits]
    link_edits = {origin_owner + "/" + path: edits for path, edits in result["edits"].items()}
    link_edits.update(inbound)

    publication = _publication_for(session, document_id)
    filename = publication.filename if publication else None
    configured = origin_configuration.get("rendering", {}).get(document_id, {})
    if filename is None:
        from .project_documents import document_output
        filename = document_output(origin_owner, {"source": old_relative, "format": declaration["format"], **configured})
    warnings = []
    if result["unresolved"]:
        warnings.append("Some referenced files were not found on the server; the document may not build: "
                        + ", ".join(sorted({u["argument"] for u in result["unresolved"]})))
    if result["preamble"].get("destination_without_preamble") and result["preamble"]["applies"]:
        warnings.append("The destination folder has no manual_style preamble; this document relies on one")
    if result["bibliography"]["unavailable"]:
        warnings.append("Citations without a bibliography entry in either Project: "
                        + ", ".join(result["bibliography"]["unavailable"]))
    if result["bibliography"]["missing"] and not result["bibliography"]["target"]:
        warnings.append("The destination's bibliography is generated; add these entries to its source: "
                        + ", ".join(e["key"] for e in result["bibliography"]["missing"]))
    if result["preamble"].get("declined"):
        warnings.append("Without the carried preamble this document will not build in the destination: it uses "
                        + ", ".join([m["name"] for m in result["preamble"]["missing"]]
                                    + [p["name"] for p in result["preamble"]["packages"] if p["required"]]))
    if any(r["required"] is False for r in result["preamble"]["packages"]):
        warnings.append("Packages loaded only by the origin's preamble and not used by this document: "
                        + ", ".join(r["name"] for r in result["preamble"]["packages"] if not r["required"]))
    origin_record = _record(session, TYPE, origin_id)
    destination_record = _record(session, TYPE, destination_id)
    value = {
        "action": ACTION,
        "document_id": document_id,
        "name": origin_configuration.get("document_names", {}).get(document_id) or PurePosixPath(old_relative).stem,
        "format": declaration["format"],
        "included": declaration["included"],
        "filename": filename,
        "source": {"project_id": origin_id, "project_name": origin_configuration["name"],
                   "folder": origin_owner, "path": old_relative,
                   "revision": origin_record.revision if origin_record else 0},
        "destination": {"project_id": destination_id, "project_name": destination_configuration["name"],
                        "folder": destination_owner, "path": new_relative,
                        "revision": destination_record.revision if destination_record else 0,
                        "collision": collision, "suggested_path": suggestion,
                        # Only for a Project without a source folder: the move creates `folder`
                        # in it, listed under the Project's name.
                        **({"new_folder": True, "folder_name": destination_configuration["name"]}
                           if new_folder is not None else {})},
        "files": files,
        "unresolved": result["unresolved"],
        "preamble": {k: v for k, v in result["preamble"].items()},
        "bibliography": result["bibliography"],
        "link_edits": link_edits,
        "markers": result["markers"],
        "warnings": warnings,
    }
    value["plan_digest"] = _structural_digest(value)
    return value


def analysis_alias(configuration: dict, declaration: dict) -> str:
    from .project_configuration import resolve
    return resolve(configuration, declaration)


def _publication_for(session, document_id: str) -> Publication | None:
    identity = session.get(DocumentIdentity, document_id)
    if identity is not None and identity.filename:
        publication = session.get(Publication, identity.filename)
        if publication is not None:
            return publication
    return session.scalar(select(Publication).where(Publication.document_id == document_id))


def declared_project_for(session, store, filename: str, project: str) -> bool:
    """A moved document's PDF is still published from its old folder until it is rebuilt.

    True when ``project``'s current configuration declares the document that
    owns ``filename`` with exactly that output, which is what a completed
    ``move_source_document`` leaves behind.
    """
    from .project_configuration import find_canonical, projection
    from .project_manifest import with_runtime_ids
    from .project_documents import document_output
    identity = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
    configuration = find_canonical(session, store, project)
    if identity is None or configuration is None:
        return False
    manifest = with_runtime_ids(projection(configuration[1], project), project)
    return any(entry["format"] in {"latex", "markdown"} and entry["document_id"] == identity.document_id
               and document_output(project, entry) == filename for entry in manifest["documents"])


def moved_from_inputs_project(session, store, filename: str, inputs_project: str) -> str | None:
    """The folder a moved document is declared in, while its newest build inputs are its old folder's.

    The desktop learns of the move from Fedora's rewritten Project
    configurations, so the Sync that moves a document can publish the build
    inputs of every other PDF before this one's for its new folder. Fedora
    builds the PDF where its Project declares it; inputs published for the old
    folder cannot be its closure there (2026-10-15: every PDF of the
    destination failed with "expected docs-concepts, not
    docs-course-notes"). None when ``inputs_project`` is still where the
    document is declared, or when no move explains the difference.
    """
    from .source_owner import owner_of
    identity = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
    folder = owner_of(identity.source_path) if identity is not None and identity.source_path else None
    if folder is None or folder == inputs_project:
        return None
    return folder if declared_project_for(session, store, filename, folder) else None


def _rewrite(value, old: str, new: str):
    """Replace exact path strings anywhere in a payload."""
    if isinstance(value, str):
        return new if value == old else value
    if isinstance(value, list):
        return [_rewrite(v, old, new) for v in value]
    if isinstance(value, dict):
        return {k: _rewrite(v, old, new) for k, v in value.items()}
    return value


def _follow_removed_proposals(session, store, operation_id: str, old_path: str, new_path: str, line_shift: int) -> int:
    """Proposals removed outside Thoughts stay restorable at their document's new path.

    Restore re-inserts the stored wrapper at ``source_file`` near
    ``removal.source_line`` (protocol/agent-proposals.md 10.5); pending
    proposals are observed as moved by reconciliation instead.
    """
    from .operations import apply_operation, latest_device_sequence
    from .proposal_decisions import REMOVED_OUTSIDE
    from .schemas import OperationInput
    timestamp = utc_now().isoformat()
    count = 0
    for entity in list(session.scalars(select(Entity).where(Entity.entity_type == "proposal", Entity.deleted.is_(False)))):
        payload = copy.deepcopy(entity.payload)
        if payload.get("status") != REMOVED_OUTSIDE or payload.get("source_file") != old_path:
            continue
        payload["source_file"] = new_path
        payload["snapshot"] = {**payload.get("snapshot", {}), "source_file": new_path}
        removal = dict(payload.get("removal") or {})
        if isinstance(removal.get("source_line"), int) and line_shift:
            removal["source_line"] += line_shift
        payload["removal"] = removal
        payload.update(revision=entity.revision + 1)
        apply_operation(session, DEVICE, OperationInput(
            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{operation_id}:proposal:{entity.entity_id}")),
            device_sequence=latest_device_sequence(session, DEVICE) + 1,
            entity_type="proposal", collection=entity.collection, entity_id=entity.entity_id,
            base_revision=entity.revision, client_timestamp=timestamp, payload=payload,
        ), provenance="document-move:" + operation_id, object_store=store)
        count += 1
    return count


def _follow_review_records(session, store, operation_id: str, old_path: str, new_path: str, destination_id: str):
    """Point every record keyed by the old source path at the new one."""
    from .logical_comments import apply_system_logical_comment
    from .operations import apply_operation, latest_device_sequence
    from .schemas import OperationInput
    if session.get(Device, DEVICE) is None:
        session.add(Device(id=DEVICE, name="Document moves", enabled=False,
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest()))
        session.flush()
    timestamp = utc_now().isoformat()
    provenance = "document-move:" + operation_id
    moved_cards = []
    counts = {"review_cards": 0, "comments": 0, "annotations": 0}
    encoded = json.dumps(old_path)
    for entity in list(session.scalars(select(Entity).where(
            Entity.entity_type.in_(("logical_comment", "annotation", "review_card")), Entity.deleted.is_(False)))):
        if encoded not in json.dumps(entity.payload):
            continue
        payload = _rewrite(copy.deepcopy(entity.payload), old_path, new_path)
        if payload == entity.payload:
            continue
        if entity.entity_type == "logical_comment":
            if "project" in payload:
                payload["project"] = destination_id
            payload.update(revision=entity.revision + 1, updated_at=timestamp)
            apply_system_logical_comment(session, payload, source_operation_id=operation_id, provenance=provenance)
            counts["comments"] += 1
            continue
        if entity.entity_type == "review_card":
            payload["project"] = destination_id
            moved_cards.append(entity.entity_id)
            counts["review_cards"] += 1
        else:
            counts["annotations"] += 1
        payload.update(revision=entity.revision + 1, updated_at=timestamp)
        apply_operation(session, DEVICE, OperationInput(
            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{operation_id}:{entity.entity_type}:{entity.collection}:{entity.entity_id}")),
            device_sequence=latest_device_sequence(session, DEVICE) + 1,
            entity_type=entity.entity_type, collection=entity.collection, entity_id=entity.entity_id,
            base_revision=entity.revision, client_timestamp=timestamp, payload=payload,
        ), provenance=provenance, object_store=store)
    if moved_cards:
        session.execute(update(ReviewReanchorJob).where(ReviewReanchorJob.card_id.in_(moved_cards),
            ReviewReanchorJob.status.in_(("queued", "retry", "running", "needs_attention"))
        ).values(status="superseded", completed_at=utc_now(), updated_at=utc_now()))
    return counts


def _edited(text: str, edits: list[dict]) -> str:
    return analysis.apply_edits(text, [analysis.Edit(e["start"], e["end"], e["replacement"], e["reason"]) for e in edits])


def move_source_document(session, store, root, request, current, record, data, digest):
    """Apply a reviewed move. The caller holds the worktree lock and checked ``base_revision``."""
    from .document_moves import finish_move
    from .library_projects import RECEIPT, TYPE, _record, _save_source, catalogue
    from .project_configuration import (add_folder, canonical_for_project, configured_manifest_path, retire_source,
                                        validate)
    from .proposal_history import reconcile_proposal_history

    operation_id = str(uuid.UUID(request["operation_id"]))
    reviewed = request.get("plan_digest")
    value = plan(session, store, root, request)
    if value["destination"]["collision"]:
        raise ValueError(f"The destination already has {value['destination']['folder']}/{value['destination']['path']}. "
                         "Choose another file name")
    if not isinstance(reviewed, str) or reviewed != value["plan_digest"]:
        raise ValueError("The files this move needs changed after you reviewed it. Choose Move to project again")
    destination_id = value["destination"]["project_id"]
    target_record = _record(session, TYPE, destination_id)
    if request.get("destination_revision") != (target_record.revision if target_record else 0):
        raise ValueError("The destination project changed on another device. Sync and try again")
    busy = session.scalar(select(AgentProposalResolution).where(
        AgentProposalResolution.status.in_(("queued", "validating"))).limit(1))
    if busy is not None:
        raise ValueError("A proposal decision is still being applied. Sync again in a moment")
    projects = {p["id"]: p for p in catalogue(session, root, store)["projects"]}
    destination = projects[destination_id]
    origin_owner, destination_owner = value["source"]["folder"], value["destination"]["folder"]
    creates_folder = bool(value["destination"].get("new_folder"))
    old_path = origin_owner + "/" + value["source"]["path"]
    new_path = destination_owner + "/" + value["destination"]["path"]
    document_id = value["document_id"]
    filename = value["filename"]
    owner_of_path = {}

    def canonical_text(path):
        row = session.get(SourceFile, path)
        if row is None or row.deleted:
            raise ValueError(f"{path} is no longer on the server. Sync and try again")
        owner_of_path[path] = row
        return store.path(row.object_sha256).read_bytes().decode("utf-8")

    previous_sources = {}
    touched = set()
    try:
        # 1. The document and its authored dependencies at their new paths.
        writes: dict[str, bytes] = {}
        retire: list[str] = []
        text = canonical_text(old_path)
        previous_sources[old_path] = owner_of_path[old_path].sha256
        text = _edited(text, value["link_edits"].get(old_path, []))
        text = analysis.insert_preamble(text, value["preamble"]["carried"])
        writes[new_path] = text.encode()
        retire.append(old_path)
        for item in value["files"]:
            if item["transfer"] != "fedora":
                continue
            source = origin_owner + "/" + item["source"]
            target = destination_owner + "/" + item["destination"]
            if item["action"] in {"move", "copy"}:
                raw = store.path(session.get(SourceFile, source).object_sha256).read_bytes()
                if source in value["link_edits"]:
                    raw = _edited(raw.decode("utf-8"), value["link_edits"][source]).encode()
                writes[target] = raw
            if item["action"] in {"move", "present-move"}:
                previous_sources[source] = session.get(SourceFile, source).sha256
                retire.append(source)
        # 2. Other documents' links to the moved document.
        for path, edits in value["link_edits"].items():
            if path == old_path or path.startswith(origin_owner + "/") and path[len(origin_owner) + 1:] in {
                    f["source"] for f in value["files"]}:
                continue
            writes[path] = _edited(canonical_text(path), edits).encode()
        # 3. Bibliography entries the destination lacks.
        bibliography = value["bibliography"]
        if bibliography["missing"] and bibliography["target"]:
            path = destination_owner + "/" + bibliography["target"]
            writes[path] = analysis.append_bibliography(canonical_text(path), bibliography["missing"], origin_owner).encode()
        for path, content in writes.items():
            existing = session.get(SourceFile, path)
            edited_in_place = path in value["link_edits"] or path == destination_owner + "/" + (bibliography["target"] or "")
            if existing is not None and not existing.deleted and not edited_in_place:
                raise ValueError(f"{path} appeared on the server after the move was planned. "
                                 "Choose Move to project again")
            _save_source(session, store, root, operation_id, path, content, materialize=False)
            touched.add(path)
        # 4. Records keyed by the source path follow it, before the old path is deleted.
        followed = {"review_cards": 0, "comments": 0, "annotations": 0}
        for old, new in [(old_path, new_path)] + [
                (origin_owner + "/" + f["source"], destination_owner + "/" + f["destination"])
                for f in value["files"] if f["action"] in {"move", "present-move"} and f["transfer"] == "fedora"]:
            for key, count in _follow_review_records(session, store, operation_id, old, new, destination_id).items():
                followed[key] += count
        if session.get(Device, DEVICE) is None:
            session.add(Device(id=DEVICE, name="Document moves", enabled=False,
                               token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest()))
            session.flush()
        carried_lines = value["preamble"]["carried"].count("\n")
        followed["removed_proposals"] = _follow_removed_proposals(session, store, operation_id, old_path, new_path,
                                                                  carried_lines)
        for path in retire:
            retire_source(session, store, root, operation_id, path, materialize=False)
            touched.add(path)

        # 5. Both Project manifests in the same transaction.
        origin_configuration = canonical_for_project(session, store, value["source"]["project_id"])
        destination_configuration = canonical_for_project(session, store, destination_id)
        declaration = next(d for d in origin_configuration["documents"] if d["document_id"] == document_id)
        rendering = copy.deepcopy(origin_configuration.get("rendering", {}).get(document_id, {}))
        name = origin_configuration.get("document_names", {}).get(document_id)
        origin_configuration["documents"] = [d for d in origin_configuration["documents"] if d["document_id"] != document_id]
        for field in ("rendering", "document_names"):
            origin_configuration.get(field, {}).pop(document_id, None)
        if creates_folder:
            # The Project's first source folder, declared as an imported folder is.
            destination_configuration = add_folder(destination_configuration, destination_owner,
                                                   _new_folder_declaration(destination_owner))
        folder_id = next(f["folder_id"] for f in destination_configuration["source_folders"] if f["path"] == destination_owner)
        if any(d["document_id"] == document_id for d in destination_configuration["documents"]):
            raise ValueError("The destination project already contains this document")
        destination_configuration["documents"].append({"document_id": document_id, "format": declaration["format"],
                                                       "included": declaration["included"],
                                                       "source": {"folder_id": folder_id, "path": value["destination"]["path"]}})
        rendering["output"] = filename
        # A compiler directory is folder-relative; it only carries over to the same relative place.
        if value["destination"]["path"] != value["source"]["path"]:
            rendering.pop("working_directory", None)
        destination_configuration.setdefault("rendering", {})[document_id] = rendering
        if name:
            destination_configuration.setdefault("document_names", {})[document_id] = name
        validate(origin_configuration)
        validate(destination_configuration)
        for configuration in (origin_configuration, destination_configuration):
            path = configured_manifest_path(configuration)
            _save_source(session, store, root, operation_id + ":project-configuration", path,
                         (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode(), materialize=False)
            touched.add(path)
        identity = session.get(DocumentIdentity, document_id)
        if identity is None or identity.source_path != new_path or identity.filename != filename:
            raise ValueError("The document identity did not follow the move")

        # 6. Library project records.
        old_alias, new_alias = old_path, new_path
        data.pop("source_project", None)
        data["order"] = [i for i in data.get("order", []) if i != old_alias]
        data["included_sources"] = [d for d in data.get("included_sources", []) if d.get("id") != old_alias]
        names = data.get("document_names", {})
        moved_name = names.pop(document_id, None) or names.pop(old_alias, None)
        target_data = copy.deepcopy(target_record.payload) if target_record else {
            "name": destination["name"], "imports": [], "order": []}
        target_data["order"] = [i for i in target_data.get("order", []) if i != new_alias]
        if target_data["order"]:
            target_data["order"].append(new_alias)
        target_data["removed_documents"] = [i for i in target_data.get("removed_documents", []) if i != new_alias]
        target_data["removed_identities"] = {k: v for k, v in target_data.get("removed_identities", {}).items()
                                             if k != new_alias}
        target_data["retired_filenames"] = [f for f in target_data.get("retired_filenames", []) if f != filename]
        data["retired_filenames"] = [f for f in data.get("retired_filenames", []) if f != filename]
        if moved_name:
            target_data.setdefault("document_names", {})[document_id] = moved_name
        if creates_folder:
            # Listed under the Project's name, as an import lists its folder under the import's.
            target_data.setdefault("source_folders", []).append(
                {"project": destination_owner, "folder": value["destination"]["folder_name"]})
        for project_id, existing, payload in ((value["source"]["project_id"], record, data),
                                              (destination_id, target_record, target_data)):
            if existing is None:
                existing = Entity(entity_type=TYPE, collection="library", entity_id=project_id, revision=0,
                                  deleted=False, winner_operation_id=operation_id, payload={})
                session.add(existing)
            existing.revision += 1
            existing.payload = payload
            existing.winner_operation_id = operation_id

        # 7. Proposal history observes a move, not a deletion.
        session.flush()
        reconcile_proposal_history(session, store, previous_sources, source_operation_id=operation_id)

        receipt = Entity(entity_type=RECEIPT, collection="library", entity_id=operation_id, revision=1, deleted=False,
                         winner_operation_id=operation_id, payload={
                             "sha256": digest, "move_pending": True, "source_paths": sorted(touched),
                             "projects": [value["source"]["project_id"], destination_id],
                             "moved_document": {"document_id": document_id, "from": old_path, "to": new_path,
                                                "filename": filename, "followed": followed,
                                                **({"created_folder": destination_owner} if creates_folder else {})}})
        session.add(receipt)
        from .library import store_published_source_index
        store_published_source_index(session, store)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finish_move(session, store, root, receipt)
    return catalogue(session, root, store)
