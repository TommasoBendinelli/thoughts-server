# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit, backed-up cutover from per-folder settings to one Project manifest.

Only Projects supplied by the active catalogue are migrated. Source directories
are never used to discover or resurrect Project membership.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from . import project_configuration as pc


def digest(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError("Migration refuses symbolic metadata files")
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def plan(root: Path, projects: list[dict], *, canonical: dict[str, dict] | None = None) -> dict:
    owners, configurations, originals, retired = {}, {}, {}, []
    for project in projects:
        target = pc.manifest_path(project["id"])
        if (root / target).exists():
            existing = pc.read(root / target)
            configurations[target] = existing
            originals[target] = digest(root / target)
            continue
        bindings = list(dict.fromkeys(filter(None, [project.get("source_project"),
                         *(folder["project"] for folder in project.get("source_folders", []))])))
        manifests = {}
        for owner in bindings:
            if owner in owners and owners[owner] != project["id"]:
                raise ValueError("Source folder belongs to multiple Projects: " + owner)
            owners[owner] = project["id"]
            relative = owner + "/thoughts-project.json"
            path = root / relative
            manifests[owner] = canonical[relative] if canonical is not None else json.loads(path.read_bytes())
            originals[relative] = digest(path)
            retired.append(relative)
        configuration = pc.from_legacy(project, manifests)
        # Inputs must already exist with the exact registered bytes. Migration
        # must never fill a missing input using a similarly named PDF elsewhere.
        for document in configuration["documents"]:
            if document["format"] == "pdf":
                path = root / pc.resolve(configuration, document)
                if digest(path) != configuration["rendering"][document["document_id"]]["sha256"]:
                    raise ValueError("Project-owned PDF input is missing or differs: " + str(path))
        configurations[target] = configuration
        originals[target] = None
        previous = str(Path(pc.default_folder(project["id"])).parent / pc.FILENAME)
        if (root / previous).exists():
            originals[previous] = digest(root / previous)
            retired.append(previous)
    return {"schema_version": 1, "configurations": configurations, "original_sha256": originals,
            "retired": sorted(set(retired)), "project_count": len(projects),
            "document_count": sum(len(c["documents"]) for c in configurations.values())}


def preflight(root: Path, migration: dict, backup: Path) -> None:
    if backup.exists():
        raise ValueError("Choose a new backup directory for this migration")
    for relative, expected in migration["original_sha256"].items():
        if digest(root / relative) != expected:
            raise ValueError("Configuration changed after preview: " + relative)
    for relative, configuration in migration["configurations"].items():
        pc.validate(configuration)
        if relative != pc.configured_manifest_path(configuration):
            raise ValueError("Invalid migration destination")
    backup.mkdir(parents=True)
    (backup / "migration.json").write_text(json.dumps(migration, ensure_ascii=False, indent=2) + "\n")
    for relative, expected in migration["original_sha256"].items():
        if expected is not None:
            destination = backup / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, destination)


def apply_local(root: Path, migration: dict, backup: Path) -> dict:
    preflight(root, migration, backup)
    try:
        for configuration in migration["configurations"].values():
            pc.save(root, configuration)
        for relative in migration["retired"]:
            (root / relative).unlink(missing_ok=True)
        for relative, expected in migration["configurations"].items():
            if pc.read(root / relative) != expected:
                raise ValueError("Migration verification failed: " + relative)
    except Exception:
        for relative, previous in migration["original_sha256"].items():
            if previous is None:
                (root / relative).unlink(missing_ok=True)
            else:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / "files" / relative, root / relative)
        raise
    return {"projects": migration["project_count"], "documents": migration["document_count"], "backup": str(backup)}


def apply_server(session, store, root: Path, migration: dict, backup: Path, operation_id: str) -> dict:
    """Caller stops writers and holds the worktree lock for cutover and verification."""
    from .library_projects import _save_source
    from .sources import _materialize_canonical_path
    preflight(root, migration, backup)
    try:
        session.info["manifest_migration_preserve_identities"] = True
        for relative, configuration in migration["configurations"].items():
            _save_source(session, store, root, operation_id, relative,
                         (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode(), materialize=False)
        for relative in migration["retired"]:
            pc.retire_source(session, store, root, operation_id, relative, materialize=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.info.pop("manifest_migration_preserve_identities", None)
    for relative in migration["configurations"]:
        _materialize_canonical_path(session, store, root, relative, already_locked=True)
    for relative in migration["retired"]:
        (root / relative).unlink(missing_ok=True)
    return {"projects": migration["project_count"], "documents": migration["document_count"], "backup": str(backup)}
