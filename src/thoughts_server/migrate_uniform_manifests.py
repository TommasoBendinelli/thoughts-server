# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Plan and apply the uniform manifest cutover with a reusable identity receipt.

The caller stops writers and backs up client/server state before applying.
Historical source objects and PDF publications are never rewritten.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from . import legacy_project_manifest_migration as pm
from .migrate_project_manifests import atomic_write
from .uniform_manifest import convert, validate


def plan(root: Path, *, identities: dict[str, str], project_keys: dict[str, str],
         canonical: dict | None = None, pdf_objects: dict[str, Path] | None = None):
    """Return exact before/after bytes, PDF copies, names, and Project ordering."""
    identities = copy.deepcopy(identities)
    pdf_objects = pdf_objects or {}
    changes, copies, names, orders = {}, {}, {}, {}

    def resolve(key, existing):
        known = identities.get(key)
        if existing and known and existing != known:
            raise ValueError(f"Document identity conflict: {key}")
        result = existing or known or str(uuid.uuid4())
        identities[key] = result
        return result

    paths = sorted(canonical) if canonical is not None else [str(p.relative_to(root)) for p in sorted(root.glob("docs-*/thoughts-project.json"))]
    originals = {relative: canonical[relative] if canonical is not None else json.loads((root / relative).read_bytes()) for relative in paths}
    pdf_owners = {}
    for relative, original in originals.items():
        owner = Path(relative).parts[0]
        project_key = project_keys.get(owner, "source:" + owner)
        for entry in original.get("documents", []):
            identity = entry.get("id", "")
            if not identity.startswith("third_party/"):
                continue
            priority = (project_key != "source:" + owner, entry["format"] != "pdf", owner)
            pair = (project_key, identity)
            if pair not in pdf_owners or priority < pdf_owners[pair][0]:
                pdf_owners[pair] = (priority, owner)
    for relative in paths:
        owner = Path(relative).parts[0]
        before = originals[relative]
        if before.get("schema_version") == 4:
            validate(before, owner)
            continue
        value = pm.with_runtime_ids(pm.validate(copy.deepcopy(before), owner), owner)
        project_key = project_keys.get(owner, "source:" + owner)
        historical_order = [d["id"] for d in value["documents"]]
        value["documents"] = [d for d in value["documents"] if not d["id"].startswith("third_party/")
                              or pdf_owners[(project_key, d["id"])][1] == owner]

        def fingerprint(filename, expected):
            candidates = [root / "third_party" / filename, root / ".thoughts/project-imports" / filename]
            if expected and expected in pdf_objects:
                candidates.insert(0, pdf_objects[expected])
            if filename in pdf_objects:
                candidates.append(pdf_objects[filename])
            if expected:
                candidates.append(root / "current_pdfs" / filename)
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                with candidate.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if expected and expected != digest:
                    continue
                from .project_configuration import default_folder
                destination = f"{default_folder(project_key)}/{filename}"
                previous = copies.get(destination)
                if previous and previous["sha256"] != digest:
                    raise ValueError(f"Conflicting Project PDF inputs: {destination}")
                copies[destination] = {"source": str(candidate), "sha256": digest}
                return digest
            raise ValueError(f"Verified PDF input unavailable for {owner}: {filename}")

        after, migrated_names, _ = convert(value, owner, project_key,
            resolve_identity=resolve, pdf_fingerprint=fingerprint)
        names.setdefault(project_key, {}).update(migrated_names)
        # Prefer the primary source folder's combined order over secondary copies.
        if (project_key not in orders or project_key == "source:" + owner
                or (not project_key.startswith("source:") and len(historical_order) > len(orders[project_key]))):
            orders[project_key] = historical_order
        changes[relative] = {"before": before, "after": after}
    return {"schema_version": 1, "operation_id": str(uuid.uuid4()), "root": str(root),
            "identities": identities, "manifests": changes, "pdfs": copies,
            "document_names": names, "orders": orders}


def apply_files(root: Path, receipt: dict, backup: Path):
    """Install verified copies and manifests atomically; retry identical plans."""
    for relative, change in receipt["manifests"].items():
        path = root / relative
        current = json.loads(path.read_bytes())
        if current not in (change["before"], change["after"]):
            raise ValueError(f"Manifest changed after migration planning: {relative}")
        if current == change["before"]:
            atomic_write(backup / relative, path.read_bytes())
    for relative, item in receipt["pdfs"].items():
        source = Path(item["source"])
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError(f"PDF changed after migration planning: {source}")
        destination = root / relative
        if destination.is_file() and destination.read_bytes() != content:
            raise ValueError(f"Managed PDF destination already has different bytes: {relative}")
        atomic_write(destination, content)
    for relative, change in receipt["manifests"].items():
        atomic_write(root / relative, (json.dumps(change["after"], ensure_ascii=False, indent=2) + "\n").encode())
    atomic_write(backup / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())


def apply_canonical(session, store, root: Path, receipt: dict, backup: Path):
    """Record migrated definitions in the canonical revision log while offline."""
    from .library_projects import TYPE
    from .legacy_project_manifest_migration import save_source as _save_source
    from .models import Entity, SourceFile
    from .document_identity import register_document
    for relative, change in receipt["manifests"].items():
        record = session.get(SourceFile, relative)
        current = json.loads(store.path(record.object_sha256).read_bytes()) if record and not record.deleted else None
        if current not in (change["before"], change["after"]):
            raise ValueError(f"Canonical manifest changed after preview: {relative}")
    # Files are backed up and checked before any canonical record is committed.
    apply_files(root, receipt, backup)
    previous_preservation = session.info.get("manifest_migration_preserve_identities", False)
    session.info["manifest_migration_preserve_identities"] = True
    try:
        for relative, change in receipt["manifests"].items():
            existing = session.get(SourceFile, relative)
            if existing and not existing.deleted and json.loads(store.path(existing.object_sha256).read_bytes()) == change["after"]:
                continue
            for document in change["after"]["documents"]:
                if document["format"] == "pdf":
                    register_document(session, document["document_id"], Path(document["source"]).name)
            _save_source(session, store, root, receipt["operation_id"], relative,
                (json.dumps(change["after"], ensure_ascii=False, indent=2) + "\n").encode(), materialize=False)
        for project_key, names in receipt["document_names"].items():
            record = session.get(Entity, (TYPE, "library", project_key))
            if record is None and not names:
                continue
            if record is None:
                owner = project_key.removeprefix("source:")
                record = Entity(entity_type=TYPE, collection="library", entity_id=project_key,
                    revision=0, deleted=False, winner_operation_id=receipt["operation_id"],
                    payload={"name": owner.removeprefix("docs-"), "source_project": owner,
                             "imports": [], "source_folders": []})
                session.add(record)
            data = copy.deepcopy(record.payload)
            for identifier, title in names.items():
                data.setdefault("document_names", {}).setdefault(identifier, title)
            historical = receipt["orders"].get(project_key, [])
            data["order"] = list(dict.fromkeys(historical + data.get("order", [])))
            if data != record.payload:
                record.payload = data
                record.revision += 1
                record.winner_operation_id = receipt["operation_id"]
        session.commit()
    except BaseException:
        session.rollback()
        for relative in receipt["manifests"]:
            original = backup / relative
            if original.is_file():
                atomic_write(root / relative, original.read_bytes())
        raise
    finally:
        session.info["manifest_migration_preserve_identities"] = previous_preservation
    bases = {}
    for relative in receipt["manifests"]:
        record = session.get(SourceFile, relative)
        bases[relative] = {"revision": record.revision, "sha256": record.sha256, "object_sha256": record.object_sha256}
    return bases
