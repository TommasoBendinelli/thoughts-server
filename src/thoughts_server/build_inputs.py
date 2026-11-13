# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BuildInputSnapshot, BuildJob, ObjectRecord, Publication, SourceFile
from .object_store import ObjectStore
from .schemas import BuildInputSnapshotRecord, BuildInputSnapshotRequest
from .trace import current_trace_id, current_trace_is_explicit
from .trace_events import append_trace_event


class BuildInputError(ValueError):
    pass


def _manifest_bytes(
    target: str,
    entries: list[dict],
    *,
    schema_version: int = 3,
    project: str | None = None,
    discovery_policy: str | None = None,
) -> bytes:
    if schema_version != 3:
        raise BuildInputError("normal build-input publication requires schema version three")
    payload = {"schema_version": schema_version, "target": target, "entries": entries}
    payload.update({"project": project, "discovery_policy": discovery_policy})
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def register_build_input_snapshot(
    session: Session,
    store: ObjectStore,
    device_id: str,
    request: BuildInputSnapshotRequest,
    *,
    trace_owner_device_id: str | None = None,
    commit: bool = True,
) -> BuildInputSnapshotRecord:
    trace_id = current_trace_id()
    entries = [
        entry.model_dump(mode="json", exclude_none=True)
        for entry in sorted(request.entries, key=lambda e: e.path)
    ]
    publication = session.get(Publication, request.target)
    registered_unbuilt = False
    if publication is None:
        from .models import DocumentIdentity
        from .project_configuration import find_canonical, projection
        from .project_manifest import with_runtime_ids
        from .project_documents import document_output
        configuration = find_canonical(session, store, request.project)
        identity = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == request.target))
        if identity is not None and configuration is not None:
            manifest = with_runtime_ids(projection(configuration[1], request.project), request.project)
            registered_unbuilt = any(entry["format"] in {"latex", "markdown"}
                and entry["document_id"] == identity.document_id
                and document_output(request.project, entry) == request.target
                for entry in manifest["documents"])
    if publication is not None and publication.project != request.project:
        # A moved source document stays published from its old folder until rebuilt.
        from .source_document_moves import declared_project_for
        if declared_project_for(session, store, request.target, request.project):
            registered_unbuilt = True
            publication = None
    if (publication is None and not registered_unbuilt) or (publication is not None and publication.project != request.project):
        raise BuildInputError("build input snapshot does not match the Library source project")
    for entry in entries:
        parts = Path(entry["path"]).parts
        role = entry["role"]
        from .source_resources import is_html_source_path, validate_html_file
        html = is_html_source_path(entry["path"], request.project)
        from .source_owner import owner_of
        if role in {"asset", "generated"} and (len(parts) < 2 or owner_of(entry["path"]) != request.project
                                               or entry["path"] == request.project):
            raise BuildInputError(f"build input escapes its source folder: {entry['path']}")
        from .uniform_manifest import managed_pdf_path
        managed_pdf = managed_pdf_path(entry["path"])
        if role == "external_pdf" and not managed_pdf and (
            len(parts) != 2 or parts[0] != "third_party" or Path(parts[1]).suffix.casefold() != ".pdf"
        ):
            raise BuildInputError(f"external PDF input is not a direct registered PDF: {entry['path']}")
        if managed_pdf:
            if role != "external_pdf":
                raise BuildInputError("Managed PDF inputs require the external_pdf role")
            from .uniform_manifest import input_path
            from .project_manifest import load_canonical, for_storage
            manifest = for_storage(load_canonical(session, store, Path("."), request.project))
            selected = {input_path(request.project, d["source"], "pdf"): d for d in manifest.get("documents", [])
                        if manifest.get("schema_version") == 4 and d.get("format") == "pdf" and d.get("included")}
            document = selected.get(entry["path"])
            if not document or manifest.get("rendering", {}).get(document["document_id"], {}).get("sha256") != entry["sha256"]:
                raise BuildInputError("Managed PDF input does not match this Project's selected document")
        record = session.get(ObjectRecord, entry["sha256"])
        if record is None or not store.path(record.sha256).is_file():
            raise BuildInputError(f"build input object is unavailable: {entry['path']}")
        if record.size_bytes != entry["size_bytes"]:
            raise BuildInputError(f"build input size does not match its object: {entry['path']}")
        if html:
            try:
                validate_html_file(store.path(record.sha256))
            except (ValueError, UnicodeError) as error:
                raise BuildInputError(f"Invalid UTF-8 HTML source: {entry['path']}: {error}") from error
        source = session.get(SourceFile, entry["path"])
        if source is not None and not source.deleted:
            raise BuildInputError(f"build input overlaps canonical source: {entry['path']}")
    content = _manifest_bytes(
        request.target,
        entries,
        schema_version=request.schema_version,
        project=request.project,
        discovery_policy=request.discovery_policy,
    )
    digest = hashlib.sha256(content).hexdigest()
    existing = session.get(BuildInputSnapshot, digest)
    if existing is None:
        manifest = store.put_bytes(
            session,
            content,
            "application/vnd.thoughts.build-input-snapshot+json",
        )
        existing = BuildInputSnapshot(
            sha256=digest,
            schema_version=request.schema_version,
            target=request.target,
            project=request.project,
            discovery_policy=request.discovery_policy,
            manifest_object_sha256=manifest.sha256,
            entries_json=entries,
            created_by=device_id,
        )
        session.add(existing)
    elif (
        existing.schema_version != request.schema_version
        or existing.target != request.target
        or existing.project != request.project
        or existing.discovery_policy != request.discovery_policy
        or existing.entries_json != entries
    ):
        raise BuildInputError("build input snapshot identity was reused with different content")
    if trace_id and current_trace_is_explicit():
        total_bytes = sum(entry["size_bytes"] for entry in entries)
        append_trace_event(
            session,
            trace_id=trace_id,
            # Internal imports attribute source records to a service identity,
            # while progress still belongs to the authenticated Sync caller.
            owner_device_id=trace_owner_device_id if trace_owner_device_id is not None else device_id,
            event_key=f"build-input-snapshot:{digest}",
            component="source_publisher",
            phase="staging",
            state="preflight",
            target=request.target,
            message=f"The server verified {len(entries)} build inputs ({total_bytes} bytes).",
            completed=len(entries),
            total=len(entries),
            unit="files",
            bytes_completed=total_bytes,
            bytes_total=total_bytes,
            metadata={
                "snapshot_sha256": digest,
                "schema_version": request.schema_version,
                "project": request.project,
                "discovery_policy": request.discovery_policy,
            },
        )
    if commit:
        session.commit()
    else:
        session.flush()
    return BuildInputSnapshotRecord(
        schema_version=existing.schema_version,
        sha256=existing.sha256,
        target=existing.target,
        project=existing.project,
        discovery_policy=existing.discovery_policy,
        manifest_object_sha256=existing.manifest_object_sha256,
        entries=existing.entries_json,
    )


def latest_build_input_snapshot(session: Session, target: str) -> BuildInputSnapshot | None:
    return session.scalar(
        select(BuildInputSnapshot)
        .where(BuildInputSnapshot.target == target, BuildInputSnapshot.schema_version == 3)
        .order_by(BuildInputSnapshot.created_at.desc())
        .limit(1)
    )


def latest_build_input_identities(session: Session) -> dict[str, dict]:
    ranked = select(
        BuildInputSnapshot.target, BuildInputSnapshot.sha256, BuildInputSnapshot.project,
        func.row_number().over(partition_by=BuildInputSnapshot.target,
                               order_by=BuildInputSnapshot.created_at.desc()).label("rank"),
    ).where(BuildInputSnapshot.schema_version == 3).subquery()
    return {row.target: {"target": row.target, "sha256": row.sha256, "project": row.project}
            for row in session.execute(select(ranked).where(ranked.c.rank == 1))}


def _latest_build_input_snapshot_any_schema(
    session: Session, target: str
) -> BuildInputSnapshot | None:
    return session.scalar(
        select(BuildInputSnapshot)
        .where(BuildInputSnapshot.target == target)
        .order_by(BuildInputSnapshot.created_at.desc())
        .limit(1)
    )


def _legacy_build_input_role(project: str, path: str) -> str:
    parts = Path(path).parts
    if parts[:3] == (project, "tex", "assets"):
        return "asset"
    if parts[:3] == (project, "tex", "generated"):
        return "generated"
    from .uniform_manifest import managed_pdf_path
    if managed_pdf_path(path) or (len(parts) == 2 and parts[0] == "third_party" and Path(parts[1]).suffix.casefold() == ".pdf"):
        return "external_pdf"
    raise BuildInputError(f"legacy build input has no canonical version-three role: {path}")


def migrate_legacy_build_input_snapshots(
    session: Session,
    store: ObjectStore,
    device_id: str,
    *,
    commit: bool = False,
) -> dict:
    """Create v3 replacements for legacy snapshots and repoint active publications."""

    snapshots = list(session.scalars(select(BuildInputSnapshot)))
    schema_by_sha256 = {snapshot.sha256: snapshot.schema_version for snapshot in snapshots}
    historical_counts = {
        str(schema): sum(snapshot.schema_version == schema for snapshot in snapshots)
        for schema in (1, 2, 3)
    }
    publication_references = sum(
        schema_by_sha256.get(snapshot_sha256) in {1, 2}
        for snapshot_sha256 in session.scalars(
            select(Publication.input_snapshot_sha256).where(
                Publication.input_snapshot_sha256.is_not(None)
            )
        )
    )
    build_references = sum(
        schema_by_sha256.get(snapshot_sha256) in {1, 2}
        for snapshot_sha256 in session.scalars(
            select(BuildJob.input_snapshot_sha256).where(
                BuildJob.input_snapshot_sha256.is_not(None)
            )
        )
    )
    publications = {
        publication.filename: publication
        for publication in session.scalars(select(Publication).order_by(Publication.filename))
    }
    active_legacy_sha256s = {
        snapshot.sha256
        for target in publications
        if (snapshot := _latest_build_input_snapshot_any_schema(session, target)) is not None
        and snapshot.schema_version in {1, 2}
    }
    active_legacy_sha256s.update(
        publication.input_snapshot_sha256
        for publication in publications.values()
        if schema_by_sha256.get(publication.input_snapshot_sha256) in {1, 2}
    )
    migrated: list[dict] = []
    errors: list[dict] = []
    legacy_snapshots = sorted(
        (snapshot for snapshot in snapshots if snapshot.schema_version in {1, 2}),
        key=lambda snapshot: (snapshot.target, snapshot.created_at, snapshot.sha256),
    )
    for snapshot in legacy_snapshots:
        publication = publications.get(snapshot.target)
        try:
            if publication is None:
                raise BuildInputError("legacy build input has no current Library publication")
            project = snapshot.project or publication.project
            if not isinstance(project, str):
                raise BuildInputError("legacy build input has no source project")
            entries = [
                {
                    **entry,
                    "role": _legacy_build_input_role(project, entry["path"]),
                }
                for entry in snapshot.entries_json
            ]
            request = BuildInputSnapshotRequest(
                schema_version=3,
                target=publication.filename,
                project=project,
                discovery_policy="tex-closure-v1",
                entries=entries,
            )
            replacement = (
                register_build_input_snapshot(session, store, device_id, request)
                if commit
                else None
            )
            publication_reference_updated = (
                publication.input_snapshot_sha256 == snapshot.sha256
            )
            if commit and publication_reference_updated and replacement is not None:
                publication.input_snapshot_sha256 = replacement.sha256
            migrated.append(
                {
                    "target": publication.filename,
                    "from_schema_version": snapshot.schema_version,
                    "from_sha256": snapshot.sha256,
                    "to_sha256": replacement.sha256 if replacement is not None else None,
                    "publication_reference_updated": publication_reference_updated,
                }
            )
        except (BuildInputError, KeyError, TypeError, ValueError) as error:
            errors.append({"target": snapshot.target, "error": str(error)})
    if commit:
        session.commit()
    return {
        "commit": commit,
        "historical_schema_counts": historical_counts,
        "historical_publication_references_to_v1_v2": publication_references,
        "historical_build_references_to_v1_v2": build_references,
        "active_legacy_snapshots": len(active_legacy_sha256s),
        "migrated": migrated,
        "errors": errors,
        "legacy_importer_removable": (
            historical_counts["1"] == 0
            and historical_counts["2"] == 0
            and publication_references == 0
            and build_references == 0
        ),
    }


def materialize_build_inputs(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    snapshot_sha256: str | None,
    *,
    include_external_pdfs: bool = True,
) -> int:
    if snapshot_sha256 is None:
        return 0
    return materialize_build_input_snapshots(
        session,
        store,
        worktree_root,
        [snapshot_sha256],
        include_external_pdfs=include_external_pdfs,
    )


def materialize_build_input_snapshots(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    snapshot_sha256s: list[str],
    *,
    include_external_pdfs: bool = True,
) -> int:
    snapshots, entries_by_path = merge_build_input_snapshots(session, snapshot_sha256s)

    root = worktree_root.resolve()
    state_root = root / ".runtime" / "server-build-inputs"
    state_root.mkdir(parents=True, exist_ok=True)
    state_path = state_root / "active.json"
    prior_paths = set()
    for prior_state_path in state_root.glob("*.json"):
        try:
            prior = json.loads(prior_state_path.read_text(encoding="utf-8"))
            prior_paths.update(prior.get("paths", []))
        except (OSError, json.JSONDecodeError):
            continue
    if not include_external_pdfs:
        entries_by_path = {path: entry for path, entry in entries_by_path.items() if entry.get("role") != "external_pdf"}
        # A cache lifecycle must never delete old user-managed PDF originals.
        from .uniform_manifest import managed_pdf_path
        prior_paths = {path for path in prior_paths if not path.startswith("third_party/") and not managed_pdf_path(path)}
    current_paths = set(entries_by_path)
    # Authored generated files can move from a binary snapshot into canonical
    # source tracking. Binary cleanup must never remove those source bytes.
    canonical_sources = set(session.scalars(select(SourceFile.path).where(SourceFile.deleted.is_(False))))
    v3_projects = {
        snapshot.project
        for snapshot in snapshots
        if snapshot.schema_version == 3 and isinstance(snapshot.project, str)
    }
    for project in sorted(v3_projects):
        role_paths = ["tex/assets", "tex/generated"]
        if any(s.project == project and s.discovery_policy == "source-closure-v2" for s in snapshots):
            role_paths.append("tex/source")
            role_paths.append("markdown/assets")
            role_paths.append("markdown/source")
        for role_path in role_paths:
            root_path = root / project / role_path
            if not root_path.is_dir():
                continue
            for candidate in root_path.rglob("*"):
                if not candidate.is_file():
                    continue
                if role_path == "markdown/source":
                    from .project_documents import is_markdown_asset
                    if not is_markdown_asset(candidate.as_posix()):
                        continue
                if role_path == "tex/source":
                    from .project_documents import is_latex_source_asset
                    if not is_latex_source_asset(candidate.relative_to(root).as_posix()):
                        continue
                relative = candidate.relative_to(root).as_posix()
                if relative not in current_paths and relative not in canonical_sources:
                    candidate.unlink()
    for relative in sorted(prior_paths - current_paths - canonical_sources):
        destination = (root / relative).resolve()
        if destination.is_relative_to(root):
            with suppress(FileNotFoundError):
                destination.unlink()
    for entry in entries_by_path.values():
        destination = (root / entry["path"]).resolve()
        if not destination.is_relative_to(root):
            raise BuildInputError(f"build input escapes worktree: {entry['path']}")
        source = store.path(entry["sha256"])
        if not source.is_file() or source.stat().st_size != entry["size_bytes"]:
            raise BuildInputError(f"build input object failed verification: {entry['path']}")
        if destination.is_file() and destination.stat().st_size == entry["size_bytes"]:
            digest = hashlib.sha256()
            with destination.open("rb") as current:
                while chunk := current.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() == entry["sha256"]:
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        try:
            digest = hashlib.sha256()
            with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if digest.hexdigest() != entry["sha256"]:
                raise BuildInputError(f"build input digest failed verification: {entry['path']}")
            os.replace(temporary, destination)
        finally:
            with suppress(FileNotFoundError):
                Path(temporary).unlink()
    for prior_state_path in state_root.glob("*.json"):
        if prior_state_path != state_path:
            with suppress(FileNotFoundError):
                prior_state_path.unlink()
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "snapshot_sha256s": [snapshot.sha256 for snapshot in snapshots],
                "paths": sorted(current_paths),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return len(current_paths)


def merge_build_input_snapshots(
    session: Session,
    snapshot_sha256s: list[str],
) -> tuple[list[BuildInputSnapshot], dict[str, dict]]:
    snapshots = []
    for snapshot_sha256 in dict.fromkeys(snapshot_sha256s):
        snapshot = session.get(BuildInputSnapshot, snapshot_sha256)
        if snapshot is None:
            raise BuildInputError("build input snapshot does not exist")
        if snapshot.schema_version != 3:
            raise BuildInputError(
                "legacy build input snapshot is unavailable to normal runtime; "
                "use the explicit migration command"
            )
        snapshots.append(snapshot)
    snapshots.sort(key=lambda snapshot: (snapshot.created_at, snapshot.sha256))

    # Build-input paths name files in one shared Fedora workspace. If an older
    # per-PDF snapshot overlaps a newer one, the newest observed object is the
    # canonical byte sequence for that path. Per-document effective identities
    # retain the dependency relationship without requiring contradictory bytes
    # to coexist at one filesystem path.
    entries_by_path: dict[str, dict] = {}
    for snapshot in snapshots:
        for entry in snapshot.entries_json:
            entries_by_path[entry["path"]] = entry
    return snapshots, entries_by_path
