# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ObjectRecord, Publication, new_document_id, utc_now
from .object_store import ObjectStore

LIBRARY_INDEX_FILENAME = ".thoughts-library-index.json"
SOURCE_INDEX_FILENAME = ".thoughts-source-index.json"
RETIRED_FILENAMES_FILENAME = "retired-filenames.json"
SAFE_PDF_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$", re.IGNORECASE)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def workspace_source_path(docs_root: Path, project: str, filename: str) -> str | None:
    """Source a workspace PDF was built from, recorded when it is registered.

    A source-linked publication without one reads as a retired whole-project PDF
    (library_projects.retired_filenames) and would drop out of the Library.
    """
    from .source_owner import is_owner
    if not is_owner(project):
        return None
    from .project_documents import has_source_project, project_documents
    from .render_types import RenderError
    folder = docs_root / project
    try:
        documents = project_documents(folder, allow_missing_inputs=True) if has_source_project(folder) else []
    except (RenderError, OSError, ValueError):
        documents = []
    source = next((document.source for document in documents if document.output == filename), None)
    # An undeclared PDF is still its folder's output; the filename keeps it unique.
    return f"{project}/{source or filename}"


@dataclass(frozen=True)
class CanonicalLibraryEntry:
    filename: str
    sha256: str
    kind: str
    path: Path
    metadata: dict[str, Any]


@dataclass
class LibraryCatalogue:
    entries: list[CanonicalLibraryEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unverified_pdfs: int = 0


@dataclass
class LibraryReconcileReport:
    total: int = 0
    linked: int = 0
    unlinked: int = 0
    object_imports: int = 0
    publication_additions: int = 0
    publication_updates: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "linked": self.linked,
            "unlinked": self.unlinked,
            "object_imports": self.object_imports,
            "publication_additions": self.publication_additions,
            "publication_updates": self.publication_updates,
            "unchanged": self.unchanged,
            "errors": self.errors,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_index(path: Path, label: str, errors: list[str]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"{path}: {error}")
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        errors.append(f"{path}: unsupported {label} schema")
        return None
    if not isinstance(value.get("pdfs"), list):
        errors.append(f"{path}: {label} PDF list is not an array")
        return None
    return value


def _source_entries(path: Path, errors: list[str]) -> dict[str, dict[str, Any]]:
    index = _read_index(path, "source-index", errors)
    if index is None:
        return {}
    result: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    for position, value in enumerate(index["pdfs"], 1):
        if not isinstance(value, dict):
            errors.append(f"{path}: source PDF entry {position} is not an object")
            continue
        filename = value.get("filename")
        if not isinstance(filename, str) or not SAFE_PDF_FILENAME.fullmatch(filename):
            errors.append(f"{path}: source PDF entry {position} has an invalid filename")
            continue
        if filename.casefold() in folded:
            errors.append(f"{path}: duplicate source PDF filename: {filename}")
            continue
        folded.add(filename.casefold())
        expected = value.get("pdf_sha256")
        if expected is not None and (
            not isinstance(expected, str) or not SHA256.fullmatch(expected.casefold())
        ):
            errors.append(f"{path}: source PDF has an invalid SHA-256: {filename}")
            continue
        result[filename] = value
    return result


def load_library_catalogue(docs_root: Path) -> LibraryCatalogue:
    docs_root = docs_root.resolve()
    current_pdfs = docs_root / "current_pdfs"
    source_path = current_pdfs / SOURCE_INDEX_FILENAME
    library_path = current_pdfs / LIBRARY_INDEX_FILENAME
    catalogue = LibraryCatalogue()
    sources = _source_entries(source_path, catalogue.errors)
    if catalogue.errors:
        return catalogue

    raw_entries: list[tuple[str, str, str, str | None, bool]] = []
    has_library_index = library_path.is_file()
    if has_library_index:
        library = _read_index(library_path, "library-index", catalogue.errors)
        if library is None:
            return catalogue
        folded: set[str] = set()
        for position, value in enumerate(library["pdfs"], 1):
            if not isinstance(value, dict):
                catalogue.errors.append(
                    f"{library_path}: Library PDF entry {position} is not an object"
                )
                continue
            filename = value.get("filename")
            kind = value.get("kind")
            expected = value.get("pdf_sha256")
            display_name = value.get("display_name")
            archived = value.get("archived", False)
            if not isinstance(filename, str) or not SAFE_PDF_FILENAME.fullmatch(filename):
                catalogue.errors.append(
                    f"{library_path}: Library PDF entry {position} has an invalid filename"
                )
                continue
            if filename.casefold() in folded:
                catalogue.errors.append(f"{library_path}: duplicate Library PDF filename: {filename}")
                continue
            folded.add(filename.casefold())
            if kind not in {"source_linked", "unlinked"}:
                catalogue.errors.append(f"{library_path}: invalid Library kind for {filename}")
                continue
            if not isinstance(expected, str) or not SHA256.fullmatch(expected.casefold()):
                catalogue.errors.append(f"{library_path}: invalid Library SHA-256 for {filename}")
                continue
            if display_name is not None:
                if (
                    not isinstance(display_name, str)
                    or not display_name.strip()
                    or len(display_name.strip()) > 200
                    or any(ord(character) < 32 or ord(character) == 127 for character in display_name)
                ):
                    catalogue.errors.append(f"{library_path}: invalid Library display name for {filename}")
                    continue
                display_name = display_name.strip()
            if not isinstance(archived, bool):
                catalogue.errors.append(f"{library_path}: invalid Library archive state for {filename}")
                continue
            raw_entries.append((filename, expected.casefold(), kind, display_name, archived))
    else:
        for filename, metadata in sources.items():
            expected = metadata.get("pdf_sha256")
            if not isinstance(expected, str):
                catalogue.unverified_pdfs += 1
                continue
            raw_entries.append((filename, expected.casefold(), "source_linked", None, False))

    library_by_name = {
        filename: kind for filename, _sha256, kind, _display_name, _archived in raw_entries
    }
    for filename in sources:
        if has_library_index and filename not in library_by_name:
            catalogue.errors.append(f"{library_path}: source-linked PDF is absent: {filename}")
        elif filename in library_by_name and library_by_name[filename] != "source_linked":
            catalogue.errors.append(f"{library_path}: source-linked PDF is marked unlinked: {filename}")
    for filename, _expected, kind, _display_name, _archived in raw_entries:
        if kind == "source_linked" and filename not in sources:
            catalogue.errors.append(f"{library_path}: linked PDF is absent from the source index: {filename}")
    if catalogue.errors:
        return catalogue

    for filename, expected, kind, display_name, archived in raw_entries:
        source_metadata = sources.get(filename, {})
        source_hash = source_metadata.get("pdf_sha256")
        if isinstance(source_hash, str) and source_hash.casefold() != expected:
            catalogue.errors.append(
                f"{library_path}: Library and source hashes disagree for {filename}"
            )
            continue
        path = current_pdfs / filename
        if path.is_symlink() or not path.is_file():
            catalogue.errors.append(f"{path}: indexed PDF is not a regular file")
            continue
        try:
            with path.open("rb") as stream:
                magic = stream.read(5)
            actual = _sha256_file(path)
        except OSError as error:
            catalogue.errors.append(f"{path}: {error}")
            continue
        if magic != b"%PDF-":
            catalogue.errors.append(f"{path}: indexed object is not a PDF")
            continue
        if actual != expected:
            catalogue.errors.append(
                f"{path}: PDF hash mismatch: expected {expected}, got {actual}"
            )
            continue
        metadata = dict(source_metadata)
        metadata.update({"filename": filename, "pdf_sha256": expected, "kind": kind})
        if display_name is not None:
            metadata["display_name"] = display_name
        metadata["archived"] = archived
        catalogue.entries.append(
            CanonicalLibraryEntry(filename, expected, kind, path, metadata)
        )
    return catalogue


def explicit_publication_metadata(publication: Publication) -> dict[str, Any]:
    metadata = dict(publication.metadata_json or {})
    metadata["filename"] = publication.filename
    metadata["pdf_sha256"] = publication.object_sha256
    metadata["kind"] = metadata.get("kind") or "source_linked"
    metadata["archived"] = metadata.get("archived") is True
    return metadata


def library_unlisted_filenames(session: Session, store: ObjectStore | None) -> set[str]:
    """Publications the server keeps but a client Library does not list.

    Retired publications belong to no document any more. Publications of
    documents their Project does not select (unchecked in Manage documents)
    keep their PDF, comments and review cards so checking them again restores
    them, but they are no more part of the Library than of render-state.
    Without the object store the Project configurations cannot be read, and
    only retired publications are left out.
    """
    from .library_projects import retired_filenames, unselected_document_filenames
    return retired_filenames(session) | unselected_document_filenames(session, store)


def published_source_index_payload(session: Session, store: ObjectStore | None = None) -> dict[str, Any]:
    """The source-layout index of the Library publications.

    With the object store this is the clients' index, which must list exactly
    the Library's source-linked publications, so unchecked documents are left
    out. A build workspace reads it without the store: its renderer still
    resolves links to the kept PDF of an unchecked document.
    """
    retired = library_unlisted_filenames(session, store)
    pdfs: list[dict[str, Any]] = []
    rows = session.scalars(select(Publication).order_by(Publication.filename))
    for row in rows:
        if row.filename in retired:
            continue
        metadata = explicit_publication_metadata(row)
        if metadata["kind"] != "source_linked":
            continue
        entry = dict(metadata)
        entry.pop("kind", None)
        entry.pop("display_name", None)
        entry.pop("archived", None)
        entry.pop("delta", None)
        entry["filename"] = row.filename
        entry["pdf_sha256"] = row.object_sha256
        pdfs.append(entry)
    return {"schema_version": 1, "pdfs": pdfs}


def published_source_index_bytes(session: Session, store: ObjectStore | None = None) -> bytes:
    payload = published_source_index_payload(session, store)
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def store_published_source_index(session: Session, store: ObjectStore) -> ObjectRecord:
    return store.put_bytes(
        session,
        published_source_index_bytes(session, store),
        "application/json",
    )


def library_index_payload(session: Session) -> dict[str, Any]:
    from .library_projects import retired_filenames
    retired = retired_filenames(session)
    rows = list(session.scalars(select(Publication).order_by(Publication.filename)))
    pdfs = []
    for row in rows:
        if row.filename in retired:
            continue
        metadata = explicit_publication_metadata(row)
        kind = metadata["kind"]
        if kind not in {"source_linked", "unlinked"}:
            raise ValueError(f"invalid publication kind for {row.filename}: {kind}")
        entry = {"document_id": row.document_id, "filename": row.filename, "pdf_sha256": row.object_sha256, "kind": kind}
        display_name = metadata.get("display_name")
        if isinstance(display_name, str) and display_name.strip():
            entry["display_name"] = display_name.strip()
        entry["archived"] = metadata.get("archived") is True
        pdfs.append(entry)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "pdfs": pdfs,
    }


def write_retired_filenames(session: Session, docs_root: Path) -> bool:
    """Mirror the retired publication set for the renderer process.

    The renderer derives the source index from the publication directory, which
    keeps the files of a retired document, so it needs this set to avoid
    re-listing publications the Library no longer owns.
    """
    from .library_projects import retired_filenames
    path = docs_root.resolve() / "library" / RETIRED_FILENAMES_FILENAME
    payload = {"schema_version": 1, "filenames": sorted(retired_filenames(session))}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    if (
        isinstance(previous, dict)
        and previous.get("schema_version") == 1
        and previous.get("filenames") == payload["filenames"]
    ):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return True


def write_library_index(session: Session, docs_root: Path) -> bool:
    write_retired_filenames(session, docs_root)
    path = docs_root.resolve() / "current_pdfs" / LIBRARY_INDEX_FILENAME
    payload = library_index_payload(session)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    if (
        isinstance(previous, dict)
        and previous.get("schema_version") == 1
        and previous.get("pdfs") == payload["pdfs"]
    ):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return True


def reconcile_library(
    docs_root: Path,
    session: Session,
    store: ObjectStore,
    *,
    commit: bool,
) -> LibraryReconcileReport:
    catalogue = load_library_catalogue(docs_root)
    report = LibraryReconcileReport(errors=list(catalogue.errors))
    report.total = len(catalogue.entries)
    report.linked = sum(entry.kind == "source_linked" for entry in catalogue.entries)
    report.unlinked = sum(entry.kind == "unlinked" for entry in catalogue.entries)
    if report.errors:
        session.rollback()
        return report

    changes: list[tuple[CanonicalLibraryEntry, Publication | None, bool]] = []
    for entry in catalogue.entries:
        publication = session.get(Publication, entry.filename)
        object_record = session.get(ObjectRecord, entry.sha256)
        object_path = store.path(entry.sha256)
        object_missing = (
            object_record is None
            or object_record.size_bytes != entry.path.stat().st_size
            or not object_path.is_file()
            or _sha256_file(object_path) != entry.sha256
        )
        report.object_imports += int(object_missing)
        if publication is None:
            report.publication_additions += 1
            changes.append((entry, None, object_missing))
            continue
        current_kind = (publication.metadata_json or {}).get("kind")
        if entry.kind == "unlinked" and current_kind != "unlinked":
            report.errors.append(
                f"{entry.filename}: refusing to replace a source-linked publication with an unlinked PDF"
            )
            continue
        if entry.kind == "unlinked" and publication.object_sha256 != entry.sha256:
            report.errors.append(
                f"{entry.filename}: refusing to replace an existing unlinked PDF with different bytes"
            )
            continue
        if entry.kind == "source_linked" and current_kind not in {None, "source_linked"}:
            report.errors.append(
                f"{entry.filename}: refusing to replace an unlinked publication with a linked PDF"
            )
            continue
        desired_project = str(entry.metadata.get("project", publication.project))
        changed = (
            publication.object_sha256 != entry.sha256
            or publication.size_bytes != entry.path.stat().st_size
            or publication.project != desired_project
            or publication.metadata_json != entry.metadata
        )
        if changed:
            report.publication_updates += 1
            changes.append((entry, publication, object_missing))
        else:
            report.unchanged += 1
            if object_missing:
                changes.append((entry, publication, object_missing))
    if report.errors or not commit:
        session.rollback()
        return report

    for entry, publication, _object_missing in changes:
        record = store.ingest_file(session, entry.path, "application/pdf")
        if record.sha256 != entry.sha256:
            session.rollback()
            report.errors.append(f"{entry.filename}: object-store digest changed during reconciliation")
            return report
        if publication is None:
            from .document_identity import register_document
            identifier = new_document_id()
            register_document(session, identifier, entry.filename, workspace_source_path(
                docs_root, str(entry.metadata.get("project", "")), entry.filename))
            publication = Publication(
                filename=entry.filename,
                document_id=identifier,
                object_sha256=record.sha256,
                size_bytes=record.size_bytes,
            )
            session.add(publication)
        identity_changed = publication.object_sha256 != record.sha256
        publication.object_sha256 = record.sha256
        publication.size_bytes = record.size_bytes
        publication.project = str(entry.metadata.get("project", ""))
        publication.metadata_json = entry.metadata
        publication.published_at = utc_now()
        if identity_changed:
            publication.placement_manifest_sha256 = None
    store_published_source_index(session, store)
    session.commit()
    write_library_index(session, docs_root)
    return report
