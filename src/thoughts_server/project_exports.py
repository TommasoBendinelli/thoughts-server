# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit, durable exports of captured reading revisions.

This queue has its own worker and never participates in Sync completion.
"""
from __future__ import annotations

import json
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from .models import ObjectRecord, ProjectExport, utc_now
from .project_reading import load_reading


def export_policy(toolchain: str) -> str:
    import hashlib
    import pypdf
    return hashlib.sha256(f"project-export-v1:{toolchain}:pypdf-{pypdf.__version__}:qpdf-structure-v1".encode()).hexdigest()


def selected_document_ids(reading, document_ids) -> list[str] | None:
    """The chosen documents in reading order, or None when every document is chosen."""
    if document_ids is None:
        return None
    if (not isinstance(document_ids, list) or not document_ids
            or not all(isinstance(value, str) for value in document_ids)
            or len(set(document_ids)) != len(document_ids)):
        raise ValueError("Choose at least one document to export")
    known = [document.document_id for document in reading.documents]
    if set(document_ids) - set(known):
        raise ValueError("A chosen document is not in the last synced project revision; Sync and try again")
    selected = [identity for identity in known if identity in set(document_ids)]
    return None if len(selected) == len(known) else selected


def request_export(session, store, *, request_id: str, project_id: str, revision: str, policy: str,
                   document_ids: list[str] | None = None):
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError("Export request identity must be a UUID")
    previous = session.get(ProjectExport, request_id)
    reading = load_reading(store, revision) if previous is None or document_ids is not None else None
    selection = selected_document_ids(reading, document_ids) if reading is not None else None
    if previous is not None:
        if ((previous.project_id, previous.reading_revision, previous.policy, previous.document_ids)
                != (project_id, revision, policy, selection)):
            raise ValueError("Export request identity was already used for different inputs")
        return previous
    if reading.project_id != project_id or not reading.documents:
        raise ValueError("The requested reading revision does not belong to this nonempty project")
    row = ProjectExport(id=request_id, project_id=project_id, reading_revision=revision, policy=policy,
                        document_ids=selection)
    # Reuse only a validated export with identical captured inputs, documents and policy.
    reusable = next((candidate for candidate in session.scalars(select(ProjectExport).where(
        ProjectExport.project_id == project_id, ProjectExport.reading_revision == revision,
        ProjectExport.policy == policy, ProjectExport.status == "complete").order_by(ProjectExport.created_at.desc()))
        if candidate.document_ids == selection), None)
    if reusable is not None:
        record = session.get(ObjectRecord, reusable.object_sha256)
        if record is not None and store.matches(record.sha256, record.size_bytes):
            row.status, row.object_sha256, row.size_bytes = "complete", record.sha256, record.size_bytes
            row.completed_at = utc_now()
    session.add(row)
    session.flush()
    return row


def export_status(row, current_revision: str | None) -> dict:
    return {"id": row.id, "project_id": row.project_id, "reading_revision": row.reading_revision,
            "status": row.status, "older_revision": row.reading_revision != current_revision,
            "document_ids": row.document_ids,
            "sha256": row.object_sha256, "size_bytes": row.size_bytes, "error": row.error,
            "created_at": row.created_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None}


def assemble_export(store, reading, destination: Path, document_ids: list[str] | None = None) -> None:
    from pypdf import PdfReader, PdfWriter
    from .project_pdf_links import restore_project_links
    from .renderer_tools.publish_pdf_comments import validate_pdf_structure
    from .renderer_tools.lib import pdf_comments

    with tempfile.TemporaryDirectory(prefix="thoughts-export-") as temporary:
        directory = Path(temporary)
        readers, pages, filenames = [], [], {}
        chosen = [document for document in reading.documents
                  if document_ids is None or document.document_id in document_ids]
        if not chosen:
            raise ValueError("No chosen document is in this project revision")
        with PdfWriter() as writer:
            for document in chosen:
                source = store.path(document.pdf_sha256)
                if not store.matches(document.pdf_sha256, document.pdf_size_bytes):
                    raise ValueError("An export document failed verification")
                package_bytes = store.path(document.package_sha256).read_bytes()
                import hashlib
                if hashlib.sha256(package_bytes).hexdigest() != document.package_sha256:
                    raise ValueError("An export package failed verification")
                package = json.loads(package_bytes)
                filenames[package["filename"]] = document.document_id
                # Materialize the annotation state captured by the package, never
                # current mutable comments from a later synchronization.
                if "pdf_comments" in package:
                    sidecar = directory / f"{len(readers)}.jsonl"
                    annotated = directory / f"{len(readers)}.pdf"
                    pdf_comments.write_comments_jsonl(package["pdf_comments"], sidecar)
                    pdf_comments.write_back_pdf(source, sidecar, annotated)
                    source = annotated
                reader = PdfReader(source)
                readers.append(reader)
                if len(reader.pages) != document.page_count:
                    raise ValueError("An export document has an inconsistent page count")
                start = len(writer.pages)
                writer.append(reader, outline_item=document.title, import_outline=True)
                pages.extend((document.document_id, reader, page, start + page)
                             for page in range(document.page_count))
            restore_project_links(writer, pages, filenames)
            writer.add_metadata({"/Title": reading.title, "/ThoughtsReadingRevision": reading.revision})
            writer.write(destination)
        with PdfReader(destination) as check:
            if len(check.pages) != sum(document.page_count for document in chosen):
                raise ValueError("Combined export failed page verification")
        validate_pdf_structure(destination)


class ProjectExportWorker:
    def __init__(self, session_factory, store, state_root: Path):
        self.session_factory, self.store, self.state_root = session_factory, store, state_root

    def recover(self):
        # The service is a singleton. Its advisory process lock is acquired by
        # run_forever before recovering jobs interrupted by a process restart.
        with self.session_factory() as session:
            for row in session.scalars(select(ProjectExport).where(ProjectExport.status == "running")):
                row.status = "queued"
            session.commit()

    def run_once(self):
        with self.session_factory() as session:
            row = session.scalar(select(ProjectExport).where(ProjectExport.status == "queued")
                .order_by(ProjectExport.created_at).with_for_update(skip_locked=True).limit(1))
            if row is None:
                return False
            row.status = "running"
            identity, revision, document_ids = row.id, row.reading_revision, row.document_ids
            session.commit()
        try:
            reading = load_reading(self.store, revision)
            self.state_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="export-", dir=self.state_root) as directory:
                path = Path(directory) / "export.pdf"
                assemble_export(self.store, reading, path, document_ids)
                with self.session_factory() as session:
                    row = session.get(ProjectExport, identity)
                    obj = self.store.ingest_file(session, path, "application/pdf")
                    row.status, row.object_sha256, row.size_bytes = "complete", obj.sha256, obj.size_bytes
                    row.completed_at = utc_now()
                    session.commit()
        except Exception as error:
            with self.session_factory() as session:
                row = session.get(ProjectExport, identity)
                row.status, row.error, row.completed_at = "failed", str(error), utc_now()
                session.commit()
        return True

    def run_forever(self, idle_exit=None):
        """Work until stopped; with ``idle_exit`` (on_demand_workers.IdleExit) until out of work."""
        import fcntl
        self.state_root.mkdir(parents=True, exist_ok=True)
        with (self.state_root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.recover()
            while True:
                if self.run_once():
                    if idle_exit is not None:
                        idle_exit.worked()
                    continue
                if idle_exit is not None and idle_exit.due():
                    return
                time.sleep(1)
