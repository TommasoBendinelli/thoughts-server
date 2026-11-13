# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Background HTML edition builds: queued at PDF publication, never part of it.

The build worker keeps its finished build workspace (the exact source snapshot
the PDF was rendered from, with the PDF build's bibliography) for the documents
it published, and queues one ``HtmlEdition`` row per published PDF. A daemon
thread of the same worker process converts them one at a time, stores every
file and the manifest as immutable objects, and records ``ready`` or
``failed``. Nothing here can fail, delay or roll back a build, a publication or
a Sync: every entry point catches its own errors.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update

from ..models import BuildJob, HtmlEdition, Publication, utc_now
from . import ENTRY, MANIFEST_MEDIA_TYPE

LOGGER = logging.getLogger(__name__)
INPUTS = "html-edition-inputs"
WORK = "html-edition-work"
FONTS = "html-edition-fonts"
STATUSES = ("pending", "running", "ready", "failed")
MAX_ATTEMPTS = 2
STALE_RUNNING = timedelta(minutes=30)
WAIT_FOR_INPUTS = timedelta(minutes=15)


def inputs_root(settings, reference: str) -> Path:
    return settings.state_root / INPUTS / reference


# ------------------------------------------------------------------ queueing (build worker)

def queue_edition(session, settings, *, publication: Publication, build_id: str, workspace: Path) -> bool:
    """Queue the HTML edition of a just-published PDF; True if it needs this build's inputs.

    Called inside the publication transaction. An edition that exists (or is
    being built) for the same PDF bytes is kept; a failed one is retried from
    these inputs.
    """
    if not settings.html_editions_enabled:
        return False
    try:
        from .build import find_document

        document = find_document(workspace / publication.project, publication.filename)
        if document is None:
            return False
        row = session.get(HtmlEdition, publication.object_sha256)
        if row is not None and row.status in {"pending", "running"}:
            return False
        # A ready edition is converted again when its source files changed although the PDF
        # bytes did not (markers placed for a card or comment made in the HTML view are
        # comments in a Markdown source).
        if row is not None and row.status == "ready" and not inputs_changed(workspace, row.result_json):
            return False
        now = utc_now()
        if row is None:
            row = HtmlEdition(pdf_sha256=publication.object_sha256, created_at=now)
            session.add(row)
        row.filename = publication.filename
        row.document_id = publication.document_id
        row.project = publication.project
        row.source_path = document.source
        row.source_format = document.source_format
        row.source_snapshot_sha256 = publication.source_snapshot_sha256
        row.build_id = build_id
        row.input_ref = build_id
        row.status = "pending"
        row.error = None
        row.attempts = 0
        row.edition_sha256 = None
        row.result_json = {}
        row.next_attempt_at = now
        row.updated_at = now
        row.started_at = None
        row.completed_at = None
        return True
    except Exception:  # noqa: BLE001 - the publication must never fail because of its HTML edition
        LOGGER.exception("could not queue the HTML edition of %s", publication.filename)
        return False


_SOURCE_ATTRIBUTE = re.compile(rb'data-src(?:-lines)?="([^":]+):[0-9]')


def edition_inputs(docs_root: Path, project: str, source_path: str, index_html: bytes) -> dict[str, str]:
    """SHA-256 of the source files an edition was converted from (docs-root relative).

    The document's own source and every file its source positions name. Marker anchors live
    in these files, so a change in them needs a new edition even when the PDF bytes did not
    change.
    """
    paths = {f"{project}/{source_path}"}
    paths.update(match.decode("utf-8", "replace") for match in _SOURCE_ATTRIBUTE.findall(index_html))
    found = {}
    for relative in sorted(paths):
        path = docs_root / relative
        if ".." not in Path(relative).parts and path.is_file():
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def inputs_changed(workspace: Path, result: dict | None) -> bool:
    """True when a recorded input file of a ready edition differs in ``workspace``."""
    recorded = (result or {}).get("inputs")
    if not isinstance(recorded, dict) or not recorded:
        return False
    for relative, digest in recorded.items():
        path = workspace / relative
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return True
    return False


def keep_inputs(settings, reference: str, workspace: Path) -> None:
    """Move a finished build workspace aside for the queued editions (same file system)."""
    try:
        destination = inputs_root(settings, reference)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(workspace, destination)
    except OSError:
        LOGGER.exception("could not keep the HTML edition inputs of build %s", reference)


def copy_inputs(settings, reference: str, docs_root: Path, projects: set[str]) -> None:
    """Copy the published projects of a build that ran in the shared worktree."""
    try:
        destination = inputs_root(settings, reference)
        if destination.exists():
            shutil.rmtree(destination)
        from ..project_configuration import manifest_files

        for project in sorted(projects):
            source = docs_root / project
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, destination / project, symlinks=True,
                                ignore=shutil.ignore_patterns("pdf", ".git", "node_modules"))
        for manifest in manifest_files(docs_root):
            target = destination / manifest.relative_to(docs_root)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(manifest, target)
    except OSError:
        LOGGER.exception("could not copy the HTML edition inputs of build %s", reference)


# ------------------------------------------------------------------ state for clients

HTML_HINT_FIELDS = ("html_status", "html_edition_sha256", "html_error")


def strip_html_hints(value):
    """Remove the HTML edition state fields from a JSON value, in place, at any depth.

    Revisions and ETags of Library-like responses are computed without them:
    an edition changes state in the background without a new PDF, and clients
    must not see that as a new Library generation (docs/html-edition.md).
    """
    if isinstance(value, dict):
        for key in HTML_HINT_FIELDS:
            value.pop(key, None)
        for item in value.values():
            strip_html_hints(item)
    elif isinstance(value, list):
        for item in value:
            strip_html_hints(item)
    return value


def edition_states(session, pdf_sha256s) -> dict[str, dict]:
    wanted = sorted({sha for sha in pdf_sha256s if isinstance(sha, str)})
    states: dict[str, dict] = {}
    for start in range(0, len(wanted), 500):
        for row in session.scalars(select(HtmlEdition).where(HtmlEdition.pdf_sha256.in_(wanted[start:start + 500]))):
            states[row.pdf_sha256] = state_of(row)
    return states


def state_of(row: HtmlEdition | None) -> dict:
    """``html_status`` (pending|ready|failed), ``html_edition_sha256`` and ``html_error``."""
    if row is None:
        return {"html_status": None, "html_edition_sha256": None, "html_error": None}
    status = "pending" if row.status in {"pending", "running"} else row.status
    return {
        "html_status": status,
        "html_edition_sha256": row.edition_sha256 if status == "ready" else None,
        "html_error": row.error if status == "failed" else None,
    }


def revision_key(session) -> tuple:
    """Changes whenever any edition changes state (for response caches)."""
    from sqlalchemy import func

    return tuple(session.execute(
        select(func.count(), func.max(HtmlEdition.updated_at)).select_from(HtmlEdition)
    ).one())


def read_manifest(store, edition_sha256: str) -> dict:
    return json.loads(store.path(edition_sha256).read_bytes())


def edition_object_ids(store, edition_sha256: str) -> set[str]:
    """The manifest and every file it lists."""
    try:
        manifest = read_manifest(store, edition_sha256)
    except (OSError, ValueError):
        return {edition_sha256}
    files = manifest.get("files") if isinstance(manifest, dict) else None
    return {edition_sha256, *(value for value in (files or {}).values() if isinstance(value, str))}


# ------------------------------------------------------------------ the worker

class HtmlEditionWorker:
    def __init__(self, settings, session_factory, store):
        self.settings = settings
        self.session_factory = session_factory
        self.store = store
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start_background(self) -> None:
        if not self.settings.html_editions_enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run_forever, name="html-editions", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        with _logged():
            self.cleanup_inputs()
        while not self._stop.is_set():
            worked = False
            with _logged():
                worked = self.run_once()
            if not worked:
                self._stop.wait(max(1.0, float(self.settings.worker_poll_seconds)))

    def claim(self) -> str | None:
        now = utc_now()
        with self.session_factory() as session:
            candidate = session.scalar(
                select(HtmlEdition)
                .where(or_(
                    and_(HtmlEdition.status == "pending", HtmlEdition.next_attempt_at <= now),
                    and_(HtmlEdition.status == "running", HtmlEdition.updated_at < now - STALE_RUNNING),
                ))
                .order_by(HtmlEdition.next_attempt_at, HtmlEdition.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            changed = session.execute(
                update(HtmlEdition)
                .where(HtmlEdition.pdf_sha256 == candidate.pdf_sha256,
                       HtmlEdition.status == candidate.status,
                       HtmlEdition.updated_at == candidate.updated_at)
                .values(status="running", attempts=HtmlEdition.attempts + 1, started_at=now, updated_at=now)
            )
            session.commit()
            return candidate.pdf_sha256 if changed.rowcount == 1 else None

    def run_once(self) -> bool:
        pdf_sha256 = self.claim()
        if pdf_sha256 is None:
            return False
        try:
            self.process(pdf_sha256)
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("HTML edition of %s failed unexpectedly", pdf_sha256)
            self._finish(pdf_sha256, error=f"{type(error).__name__}: {error}", retry=True)
        finally:
            with _logged():
                self.cleanup_inputs()
        return True

    def _finish(self, pdf_sha256: str, *, error: str | None = None, retry: bool = False,
                edition_sha256: str | None = None, result: dict | None = None, wait: bool = False) -> None:
        with self.session_factory() as session:
            row = session.get(HtmlEdition, pdf_sha256)
            if row is None:
                return
            now = utc_now()
            row.updated_at = now
            if wait:
                row.status = "pending"
                row.attempts = max(0, row.attempts - 1)
                row.next_attempt_at = now + timedelta(seconds=30)
            elif error is not None and retry and row.attempts < MAX_ATTEMPTS:
                row.status = "pending"
                row.error = error[:1000]
                row.next_attempt_at = now + timedelta(seconds=60)
            elif error is not None:
                row.status = "failed"
                row.error = error[:1000]
                row.completed_at = now
            else:
                row.status = "ready"
                row.error = None
                row.edition_sha256 = edition_sha256
                row.completed_at = now
            if result is not None:
                row.result_json = result
            session.commit()

    def process(self, pdf_sha256: str) -> None:
        from ..markdown_source import MarkdownError
        from ..source_owner import short_name
        from .build import convert_document, find_document, manifest, manifest_bytes
        from .bundle import EditionInvalid, media_type
        from .latex import ConversionFailed

        with self.session_factory() as session:
            row = session.get(HtmlEdition, pdf_sha256)
            if row is None or row.status != "running":
                return
            newer = session.scalar(
                select(HtmlEdition.pdf_sha256).where(
                    HtmlEdition.filename == row.filename, HtmlEdition.created_at > row.created_at,
                    HtmlEdition.status.in_(("pending", "running", "ready")),
                ).limit(1)
            )
            build = session.get(BuildJob, row.input_ref) if row.input_ref else None
            build_running = build is not None and build.status == "running"
            snapshot = dict(filename=row.filename, project=row.project, input_ref=row.input_ref,
                            document_id=row.document_id, source_snapshot_sha256=row.source_snapshot_sha256,
                            created_at=row.created_at)
        if newer is not None:
            self._finish(pdf_sha256, error="superseded by a newer PDF before its HTML edition was built")
            return
        inputs = inputs_root(self.settings, snapshot["input_ref"] or "missing")
        if not inputs.is_dir():
            if build_running or utc_now() - _aware(snapshot["created_at"]) < WAIT_FOR_INPUTS:
                self._finish(pdf_sha256, wait=True)
            else:
                self._finish(pdf_sha256, error="the source snapshot of this PDF is no longer available; "
                                               "rebuild the document to make its HTML edition")
            return
        project = inputs / snapshot["project"]
        document = find_document(project, snapshot["filename"]) if project.is_dir() else None
        if document is None:
            self._finish(pdf_sha256, error="the document is not in its source snapshot")
            return
        stem = Path(document.output).stem.removeprefix(short_name(snapshot["project"]) + "_")
        bibliography = project / "build" / f"{stem}.bbl"
        work = self.settings.state_root / WORK / pdf_sha256
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        started = time.monotonic()
        try:
            edition = convert_document(
                inputs, project, document, work,
                bibliography=bibliography if bibliography.is_file() else None,
                sandbox=self.settings.sandbox_builds,
                timeout=self.settings.html_edition_timeout_seconds,
                font_cache=self.settings.state_root / FONTS,
            )
        except (ConversionFailed, EditionInvalid, MarkdownError, ValueError) as error:
            self._finish(pdf_sha256, error=str(error) or type(error).__name__,
                         result={"seconds": round(time.monotonic() - started, 2)})
            return
        finally:
            shutil.rmtree(work, ignore_errors=True)
        seconds = round(time.monotonic() - started, 2)
        inputs_used = edition_inputs(inputs, snapshot["project"], document.source,
                                     edition.files.get(ENTRY, b""))
        with self.session_factory() as session:
            hashes = {}
            for name, data in sorted(edition.files.items()):
                hashes[name] = self.store.put_bytes(session, data, media_type(name)).sha256
            value = manifest(edition, hashes, document_id=snapshot["document_id"], pdf_sha256=pdf_sha256,
                             source_snapshot_sha256=snapshot["source_snapshot_sha256"])
            record = self.store.put_bytes(session, manifest_bytes(value), MANIFEST_MEDIA_TYPE)
            session.commit()
        self._finish(pdf_sha256, edition_sha256=record.sha256,
                     result={"seconds": seconds, "stats": edition.stats, "anchors": len(edition.anchors),
                             "warnings": edition.warnings[:20], "inputs": inputs_used})

    def cleanup_inputs(self) -> None:
        """Remove kept build workspaces no queued edition needs any more."""
        root = self.settings.state_root / INPUTS
        if not root.is_dir():
            return
        with self.session_factory() as session:
            needed = set(session.scalars(
                select(HtmlEdition.input_ref).where(HtmlEdition.status.in_(("pending", "running")))
            ))
            running_builds = set(session.scalars(select(BuildJob.id).where(BuildJob.status == "running")))
        for candidate in root.iterdir():
            if candidate.name not in needed and candidate.name not in running_builds:
                shutil.rmtree(candidate, ignore_errors=True)


def _aware(value):
    """SQLite returns naive datetimes for timezone-aware columns."""
    from datetime import UTC

    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class _logged:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is not None and issubclass(kind, Exception):
            LOGGER.exception("HTML edition worker error")
            return True
        return False
