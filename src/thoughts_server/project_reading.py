# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Immutable project reading compositions, independent of combined PDF exports."""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

KIND = "thoughts-project-reading"
MEDIA_TYPE = "application/vnd.thoughts.project-reading+json"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid {field}")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"Invalid {field}")
    return value


@dataclass(frozen=True)
class ReadingDocument:
    document_id: str
    title: str
    pdf_sha256: str
    pdf_size_bytes: int
    page_count: int
    package_sha256: str

    @classmethod
    def parse(cls, value: dict) -> ReadingDocument:
        if not isinstance(value, dict):
            raise ValueError("Invalid reading document")
        size, pages = value.get("pdf_size_bytes"), value.get("page_count")
        if type(size) is not int or size < 1 or type(pages) is not int or not 1 <= pages <= 1_000_000:
            raise ValueError("Invalid document size or page count")
        return cls(
            _identity(value.get("document_id"), "document identity"),
            _identity(value.get("title"), "document title"),
            _digest(value.get("pdf_sha256"), "PDF fingerprint"),
            size, pages,
            _digest(value.get("package_sha256"), "package fingerprint"),
        )

    def payload(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class ProjectReading:
    project_id: str
    title: str
    documents: tuple[ReadingDocument, ...]

    @classmethod
    def parse(cls, value: dict) -> ProjectReading:
        if not isinstance(value, dict) or value.get("kind") != KIND or type(value.get("schema_version")) is not int or value.get("schema_version") != 1:
            raise ValueError("Unsupported project reading format")
        rows = value.get("documents")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("Invalid project documents")
        documents = tuple(ReadingDocument.parse(row) for row in rows)
        if len({d.document_id for d in documents}) != len(documents):
            raise ValueError("Duplicate document identity")
        sizes: dict[str, int] = {}
        for document in documents:
            if sizes.setdefault(document.pdf_sha256, document.pdf_size_bytes) != document.pdf_size_bytes:
                raise ValueError("Conflicting PDF sizes")
        result = cls(_identity(value.get("project_id"), "project identity"),
                     _identity(value.get("title"), "project title"), documents)
        if type(value.get("page_count")) is not int or result.page_count > 10_000_000 or value.get("page_count") != result.page_count:
            raise ValueError("Project page count does not match its documents")
        return result

    @property
    def page_count(self) -> int:
        return sum(d.page_count for d in self.documents)

    @property
    def pdf_size_bytes(self) -> int:
        return sum({d.pdf_sha256: d.pdf_size_bytes for d in self.documents}.values())

    def payload(self) -> dict:
        return {"schema_version": 1, "kind": KIND, "project_id": self.project_id,
                "title": self.title, "page_count": self.page_count,
                "documents": [d.payload() for d in self.documents]}

    def encode(self) -> bytes:
        payload = self.payload()
        self.parse(payload)
        return (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.encode()).hexdigest()

    def locate(self, project_page: int) -> tuple[ReadingDocument, int]:
        """Return document and local page; both page indices are zero based."""
        if type(project_page) is not int or not 0 <= project_page < self.page_count:
            raise ValueError("Project page is outside the reading composition")
        starts, total = [], 0
        for document in self.documents:
            starts.append(total)
            total += document.page_count
        index = bisect_right(starts, project_page) - 1
        return self.documents[index], project_page - starts[index]

    def project_page(self, document_id: str, local_page: int) -> int:
        start = 0
        for document in self.documents:
            if document.document_id == document_id:
                if type(local_page) is not int or not 0 <= local_page < document.page_count:
                    raise ValueError("Document page is outside its PDF")
                return start + local_page
            start += document.page_count
        raise ValueError("Document is not selected in this project")


def load_reading(store, revision: str) -> ProjectReading:
    raw = store.path(_digest(revision, "reading revision")).read_bytes()
    if hashlib.sha256(raw).hexdigest() != revision:
        raise ValueError("Reading composition failed verification")
    return ProjectReading.parse(json.loads(raw))


def publish_reading(session, store, reading: ProjectReading):
    """Publish only after every exact document and package is present and verified.

    The caller commits this pointer alongside the document publications so no
    partially built project becomes visible to a transfer plan.
    """
    from .models import ObjectRecord, ReadingPublication, utc_now

    encoded = reading.encode()
    for document in reading.documents:
        record = session.get(ObjectRecord, document.pdf_sha256)
        if (record is None or record.size_bytes != document.pdf_size_bytes
                or not store.matches(document.pdf_sha256, document.pdf_size_bytes)):
            raise ValueError("Required reading PDF is missing or corrupt")
        package_record = session.get(ObjectRecord, document.package_sha256)
        if package_record is None or not store.matches(document.package_sha256, package_record.size_bytes):
            raise ValueError("Required document package is missing or corrupt")
        package = json.loads(store.path(document.package_sha256).read_bytes())
        kind = package.get("kind")
        if kind == "thoughts-document-reading-package":
            if package.get("schema_version") != 1 or package.get("document_id") != document.document_id:
                raise ValueError("Document package identity does not match the reading")
            placement = package.get("placement_manifest_sha256")
            if placement is not None:
                placement_record = session.get(ObjectRecord, _digest(placement, "placement fingerprint"))
                if placement_record is None or not store.matches(placement, placement_record.size_bytes):
                    raise ValueError("Document placement package is missing or corrupt")
                value = json.loads(store.path(placement).read_bytes())
                if value.get("document_id") != document.document_id or value.get("pdf_sha256") != document.pdf_sha256:
                    raise ValueError("Document placement package identity does not match")
        elif kind == "thoughts-document-source-artifact":
            from .sync_plans import reader_source_dependencies
            for dependency in reader_source_dependencies(package, package["filename"],
                    include_source_resources=True, include_component_packages=True,
                    read_object=lambda identity: store.path(identity).read_bytes()):
                dependency_record = session.get(ObjectRecord, dependency.sha256)
                if dependency_record is None or not store.matches(dependency.sha256, dependency_record.size_bytes):
                    raise ValueError("Document source package is incomplete or corrupt")
        else:
            raise ValueError("Unsupported document reading package")
        pdf = package.get("pdf", package)
        if (pdf.get("pdf_sha256") != document.pdf_sha256
                or pdf.get("page_count") != document.page_count):
            raise ValueError("Document package does not describe the required PDF")
    record = store.put_bytes(session, encoded, MEDIA_TYPE)
    current = session.get(ReadingPublication, reading.project_id)
    if current is None:
        current = ReadingPublication(project_id=reading.project_id,
            filename="project_" + hashlib.sha256(reading.project_id.encode()).hexdigest()[:24] + ".thoughts-project")
        session.add(current)
    if current.revision != record.sha256:
        current.revision = record.sha256
        current.page_count = reading.page_count
        current.pdf_size_bytes = reading.pdf_size_bytes
        current.published_at = utc_now()
    return current


def reading_entries(session):
    from sqlalchemy import select
    from .models import ObjectRecord, ReadingPublication
    return [{"project_id": row.project_id, "filename": row.filename, "sha256": row.revision,
             "size_bytes": size, "page_count": row.page_count, "pdf_size_bytes": row.pdf_size_bytes,
             "published_at": row.published_at.isoformat()}
            for row, size in session.execute(select(ReadingPublication, ObjectRecord.size_bytes)
                .join(ObjectRecord, ObjectRecord.sha256 == ReadingPublication.revision)
                .order_by(ReadingPublication.project_id))]
