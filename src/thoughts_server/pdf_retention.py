# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Offline retirement of superseded, proven-generated PDF object bytes.

This maintenance operation must run with every Thoughts API and worker stopped.
Reader transfer plans live in the API process, so a live database scan cannot
prove that an older PDF is no longer being downloaded.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Base, BuildJob, Entity, HtmlEdition, ObjectRecord
from .html_edition.jobs import edition_object_ids
from .object_store import ObjectStore

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _generated_pdf_ids(session: Session) -> set[str]:
    """Require a successful build outcome, never infer provenance from MIME alone."""
    generated: set[str] = set()
    for result in session.scalars(select(BuildJob.result)):
        if not isinstance(result, dict):
            continue
        for outcome in result.get("outcomes", []):
            if not isinstance(outcome, dict) or outcome.get("status") not in {"published", "unchanged"}:
                continue
            digest = outcome.get("sha256")
            if isinstance(digest, str) and SHA256.fullmatch(digest):
                generated.add(digest)
    return generated


def _referenced_object_ids(session: Session) -> set[str]:
    """Preserve every scalar object reference, completed review jobs, and PDFs feedback was given on."""
    referenced: set[str] = set()
    for table in Base.metadata.tables.values():
        if table.name == ObjectRecord.__tablename__:
            continue
        for column in table.columns:
            if not any(key.target_fullname == "objects.sha256" for key in column.foreign_keys):
                continue
            referenced.update(
                digest for digest in session.scalars(select(column).where(column.is_not(None)).distinct())
                if isinstance(digest, str)
            )
    referenced.update(_feedback_pdf_ids(session))
    referenced.update(_card_attachment_ids(session))
    return referenced


# The PDF version a user gave feedback on stays for "Show old version". Every record
# may carry a top-level review_version {pdf_sha256, source_built} (one key for all
# record types); feedback records also name their PDF in their own fields.
REVIEW_VERSION_PATH = ("review_version", "pdf_sha256")
FEEDBACK_PDF_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    # Liked/disliked answer passages: the exact PDF that was displayed.
    "review_card_feedback": (("anchor", "pdf", "pdf_sha256"),),
    # Liked/disliked passages of a document: the PDF snapshot that was displayed.
    "document_feedback": (("anchor", "pdf", "pdf_sha256"),),
    # Comments, replies and notes: the PDF the comment was first made on.
    "logical_comment": (("origin", "pdf_sha256"),),
    # Per-PDF comment placements (and legacy per-PDF comments): the PDF their geometry is on.
    "annotation": (("pdf_sha256",), ("annotation_pdf_sha256",)),
    "comment": (("pdf_sha256",), ("annotation_pdf_sha256",)),
    # A confirmed/flagged card correspondence with its note: the PDF it was judged on.
    "review_correspondence_review": (("pdf_sha256",),),
}
# HTML editions are retained like their PDFs: the edition a record was made on in the
# HTML view (docs/html-edition.md) stays, with its PDF, as long as that record exists.
REVIEW_VERSION_HTML_PATH = ("review_version", "html_edition_sha256")
FEEDBACK_HTML_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "review_card_feedback": (("html_edition_sha256",),),
    "logical_comment": (("origin", "html_edition_sha256"),),
}


# The HTML attachment version a review card was made in (docs/HTML_ATTACHMENTS.md) stays
# while the card exists, so a client can still open the page the card was made on.
CARD_ATTACHMENT_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "review_card": (("primary", "html_attachment", "sha256"),),
    "document_feedback": (("anchor", "html_attachment", "sha256"),),
}


def _path_value(payload: object, path: tuple[str, ...]) -> object:
    for key in path:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def _feedback_pdf_ids(session: Session) -> set[str]:
    """PDFs named by user feedback: any record's review_version plus feedback records' own PDF fields.

    Deleted records count too; a delete is not new feedback and keeps what the record named.
    """
    return _stamped_ids(session, REVIEW_VERSION_PATH, FEEDBACK_PDF_PATHS)


def _feedback_html_edition_ids(session: Session) -> set[str]:
    """HTML editions named by records made in the HTML view (same rules as PDFs)."""
    return _stamped_ids(session, REVIEW_VERSION_HTML_PATH, FEEDBACK_HTML_PATHS)


def _card_attachment_ids(session: Session) -> set[str]:
    """HTML attachment objects named by review cards and document feedback made inside them."""
    return _stamped_ids(session, None, CARD_ATTACHMENT_PATHS)


def _stamped_ids(session: Session, version_path, paths) -> set[str]:
    stamped: set[str] = set()
    rows = session.execute(select(Entity.entity_type, Entity.payload).execution_options(yield_per=500))
    for entity_type, payload in rows:
        for path in ((version_path,) if version_path else ()) + tuple(paths.get(entity_type, ())):
            digest = _path_value(payload, path)
            if isinstance(digest, str) and SHA256.fullmatch(digest):
                stamped.add(digest)
    return stamped


def _verified_path(store: ObjectStore, record: ObjectRecord) -> Path | None:
    if record.relative_path != str(store.relative_path(record.sha256)):
        return None
    path = store.path(record.sha256)
    if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
        return None
    if not path.is_file() or not store.matches(record.sha256, record.size_bytes):
        return None
    return path


def _ticket_path(ticket_root: Path, digest: str) -> Path:
    return ticket_root / f"{digest}.json"


def _write_ticket(ticket_root: Path, record: ObjectRecord) -> Path:
    ticket_root.mkdir(parents=True, exist_ok=True)
    ticket = _ticket_path(ticket_root, record.sha256)
    temporary = ticket.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump({"sha256": record.sha256, "size_bytes": record.size_bytes}, output)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, ticket)
    return ticket


def _finish_tickets(session: Session, store: ObjectStore, ticket_root: Path) -> int:
    """Finish file removal after a crash between database commit and unlink."""
    completed = 0
    if ticket_root.is_symlink():
        return completed
    for ticket in ticket_root.glob("*.json") if ticket_root.is_dir() else ():
        digest = ticket.stem
        if ticket.is_symlink() or not SHA256.fullmatch(digest):
            continue
        try:
            metadata = json.loads(ticket.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if metadata.get("sha256") != digest or not isinstance(metadata.get("size_bytes"), int):
            continue
        if session.get(ObjectRecord, digest) is not None:
            # A failed or interrupted database transaction left the bytes live.
            ticket.unlink()
            continue
        path = store.path(digest)
        if path.exists():
            if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
                continue
            if not store.matches(digest, metadata["size_bytes"]):
                continue
            path.unlink()
        ticket.unlink()
        completed += 1
    return completed


def retire_superseded_pdfs(
    session: Session,
    store: ObjectStore,
    state_root: Path,
    *,
    commit: bool = False,
    min_age: timedelta = timedelta(days=1),
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Delete only older generated PDFs with no persisted object references.

    Callers must stop all server processes before commit. A ticket makes the
    database-first deletion recoverable without leaving broken object records.
    """
    now = now or datetime.now(UTC)
    ticket_root = state_root / "pdf-retirement-tickets"
    recovered = _finish_tickets(session, store, ticket_root) if commit else 0
    generated = _generated_pdf_ids(session)
    protected = _referenced_object_ids(session)
    editions = list(session.scalars(select(HtmlEdition)))
    pinned_editions = _feedback_html_edition_ids(session)
    # A pinned HTML edition keeps the PDF it was built beside.
    protected.update(row.pdf_sha256 for row in editions if row.edition_sha256 in pinned_editions)
    candidates: list[ObjectRecord] = []
    invalid = 0
    for record in session.scalars(select(ObjectRecord).where(ObjectRecord.media_type == "application/pdf")):
        if record.sha256 not in generated or record.sha256 in protected:
            continue
        created = record.created_at
        if created is None:
            continue
        created_utc = created.replace(tzinfo=UTC) if created.tzinfo is None else created
        if created_utc > now - min_age:
            continue
        if _verified_path(store, record) is None:
            invalid += 1
            continue
        candidates.append(record)
    # The HTML editions of retired PDFs go with them, unless a record pins them; a file
    # shared with an edition that stays (same figure, same stylesheet) stays too.
    retired_pdfs = {record.sha256 for record in candidates}
    retired_rows = [row for row in editions if row.pdf_sha256 in retired_pdfs]
    kept_ids: set[str] = set()
    for row in editions:
        if row.edition_sha256 and (row.pdf_sha256 not in retired_pdfs or row.edition_sha256 in pinned_editions):
            kept_ids |= edition_object_ids(store, row.edition_sha256)
    edition_ids: set[str] = set()
    for row in retired_rows:
        if row.edition_sha256 and row.edition_sha256 not in pinned_editions:
            edition_ids |= edition_object_ids(store, row.edition_sha256)
    edition_ids -= kept_ids | protected
    edition_records = [record for record in (session.get(ObjectRecord, digest) for digest in sorted(edition_ids))
                       if record is not None and _verified_path(store, record) is not None]
    result: dict[str, int | bool] = {
        "committed": commit,
        "eligible_count": len(candidates),
        "eligible_bytes": sum(record.size_bytes for record in candidates),
        "invalid_count": invalid,
        "recovered_tickets": recovered,
        "removed_count": 0,
        "removed_bytes": 0,
        "html_edition_count": sum(1 for row in retired_rows if row.edition_sha256 not in pinned_editions),
        "html_object_count": len(edition_records),
        "html_removed_count": 0,
    }
    if not commit:
        return result
    for row in retired_rows:
        if row.edition_sha256 not in pinned_editions:
            session.delete(row)
    session.commit()
    for record in edition_records:
        path = store.path(record.sha256)
        ticket = _write_ticket(ticket_root, record)
        session.delete(record)
        session.commit()
        if _verified_path(store, record) is not None:
            path.unlink()
            ticket.unlink()
            result["html_removed_count"] += 1
    for record in candidates:
        path = store.path(record.sha256)
        ticket = _write_ticket(ticket_root, record)
        session.delete(record)
        session.commit()
        if _verified_path(store, record) is not None:
            path.unlink()
            ticket.unlink()
            result["removed_count"] += 1
            result["removed_bytes"] += record.size_bytes
    return result
