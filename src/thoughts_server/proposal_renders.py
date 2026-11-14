# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import logging
from datetime import UTC, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .builds import conditional_render_check
from .config import Settings
from .object_store import ObjectStore
from .models import AgentProposalResolution, DeferredProposalRender, utc_now

PROPOSAL_RENDER_TARGET = "all"
PROPOSAL_RENDER_DEBOUNCE_SECONDS = 2.0
# An accept keeps the PDF and publishes a source equivalence at once, but that
# PDF's SyncTeX still names the removed wrapper's lines, so text inside an
# accepted proposal cannot be placed from it. Rebuild once accepts settle.
ACCEPTED_PDF_REBUILD_PREFIX = "accepted-pdf:"
ACCEPTED_PDF_REBUILD_DEBOUNCE_SECONDS = 20.0


def defer_proposal_render(
    session: Session,
    requested_by: str,
    *,
    resolution_id: str | None = None,
) -> DeferredProposalRender:
    """Create or extend the single durable idle window for proposal renders."""

    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('thoughts-proposal-render', 0))")
        )
    now = utc_now()
    row = session.get(DeferredProposalRender, PROPOSAL_RENDER_TARGET, with_for_update=True)
    if row is None:
        row = DeferredProposalRender(
            target=PROPOSAL_RENDER_TARGET,
            requested_by=requested_by,
            generation=1,
            resolution_ids_json=[resolution_id] if resolution_id else [],
            ready_at=now + timedelta(seconds=PROPOSAL_RENDER_DEBOUNCE_SECONDS),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        return row

    resolution_ids = {
        value for value in row.resolution_ids_json if isinstance(value, str) and value
    }
    if resolution_id:
        resolution_ids.add(resolution_id)
    row.requested_by = requested_by
    row.generation += 1
    row.resolution_ids_json = sorted(resolution_ids)
    row.ready_at = now + timedelta(seconds=PROPOSAL_RENDER_DEBOUNCE_SECONDS)
    row.updated_at = now
    return row


def defer_accepted_pdf_rebuild(session: Session, requested_by: str, filename: str) -> DeferredProposalRender:
    """Create or extend the idle window before rebuilding a PDF kept across an accept."""

    target = ACCEPTED_PDF_REBUILD_PREFIX + filename
    now = utc_now()
    row = session.get(DeferredProposalRender, target, with_for_update=True)
    if row is None:
        row = DeferredProposalRender(target=target, requested_by=requested_by, generation=1,
                                     resolution_ids_json=[], created_at=now)
        session.add(row)
    else:
        row.requested_by = requested_by
        row.generation += 1
    row.ready_at = now + timedelta(seconds=ACCEPTED_PDF_REBUILD_DEBOUNCE_SECONDS)
    row.updated_at = now
    return row


def _keeps_accepted_pdf(session: Session, store: ObjectStore, filename: str) -> bool:
    """Whether the published PDF still predates an accept; a later build makes the rebuild moot."""

    import json

    from .models import Publication

    publication = session.get(Publication, filename)
    if publication is None or not publication.source_artifact_sha256:
        return False
    try:
        manifest = json.loads(store.path(publication.source_artifact_sha256).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return any(
        isinstance(item, dict) and item.get("method") == "proposal_accept_metadata_v1"
        for item in manifest.get("source_revision_equivalences", [])
    )


def _enqueue_accepted_pdf_rebuild(
    session: Session, store: ObjectStore, toolchain_key: str, row: DeferredProposalRender, requested_by: str,
) -> None:
    from .builds import enqueue_build

    filename = row.target.removeprefix(ACCEPTED_PDF_REBUILD_PREFIX)
    generation = row.generation
    if _keeps_accepted_pdf(session, store, filename):
        try:
            # The request ID names the idle window, so the worker and a render check
            # that both dispatch one window share a single build.
            enqueue_build(
                session, requested_by, filename, toolchain_key,
                f"accepted-pdf:{filename}:{generation}", retry_terminal=True, commit=False, store=store,
            )
        except ValueError:
            pass  # The document was removed or renamed; its own Sync builds it.
    current = session.get(DeferredProposalRender, row.target, with_for_update=True)
    if current is not None and current.generation == generation:
        session.delete(current)


def _dispatch_accepted_pdf_rebuild(session: Session, settings: Settings, row: DeferredProposalRender) -> bool:
    _enqueue_accepted_pdf_rebuild(
        session, ObjectStore(settings.object_root), settings.toolchain_key, row, row.requested_by,
    )
    session.commit()
    return True


def dispatch_accepted_pdf_rebuilds_for_render_check(
    session: Session,
    store: ObjectStore,
    toolchain_key: str,
    target: str,
    requested_by: str,
) -> int:
    """Queue every accepted-PDF rebuild still waiting out its idle window for [target] now.

    A render check answers whether the Library's PDFs are final. While such a rebuild
    waits, the kept PDF matches the source through its equivalence, so the check said
    "current": the Sync that uploaded the accept ended "Up to date" and never followed
    the build that started 20 s later (2026-10-10 19:49). Any accepts a device made
    before checking were delivered by then, so the idle window has served its purpose.
    The build joins the asking device's trace, which then follows it to completion.
    """

    rows = [
        row
        for row in session.scalars(
            select(DeferredProposalRender).where(
                DeferredProposalRender.target.startswith(ACCEPTED_PDF_REBUILD_PREFIX)
            )
        )
        if target == PROPOSAL_RENDER_TARGET or row.target == ACCEPTED_PDF_REBUILD_PREFIX + target
    ]
    for row in rows:
        _enqueue_accepted_pdf_rebuild(session, store, toolchain_key, row, requested_by)
    if rows:
        session.commit()
    return len(rows)


def proposal_transition_requires_render(session: Session, operation_id: str) -> bool:
    from .models import Operation

    operation = session.scalar(select(Operation).where(Operation.operation_id == operation_id))
    return bool(
        operation is not None
        and operation.outcome in {"applied", "merged"}
        and operation.payload.get("proposal_transition")
        in {"accept_pdf", "disregard", "mixed", "undo_accept_pdf", "undo_disregard"}
    )


def dispatch_due_proposal_render(session: Session, settings: Settings) -> bool:
    """Materialize one due proposal render, leaving retries idempotent."""

    row = session.scalar(
        select(DeferredProposalRender)
        .where(DeferredProposalRender.ready_at <= utc_now())
        .order_by(DeferredProposalRender.ready_at)
        .limit(1)
    )
    if row is None:
        return False
    if row.target.startswith(ACCEPTED_PDF_REBUILD_PREFIX):
        return _dispatch_accepted_pdf_rebuild(session, settings, row)

    generation = row.generation
    resolution_ids = {
        value for value in row.resolution_ids_json if isinstance(value, str) and value
    }
    outcome = conditional_render_check(
        session,
        row.requested_by,
        row.target,
        settings.toolchain_key,
        request_id=f"agent-proposals:{generation}",
        store=ObjectStore(settings.object_root),
    )
    build = outcome.get("build") if isinstance(outcome, dict) else None
    build_id = build.get("id") if isinstance(build, dict) else None

    current = session.get(DeferredProposalRender, row.target, with_for_update=True)
    for resolution_id in resolution_ids:
        resolution = session.get(AgentProposalResolution, resolution_id)
        if resolution is None:
            continue
        result = dict(resolution.result_json or {})
        result["pdf_update"] = "building" if build_id else "current"
        if build_id:
            result["build_id"] = build_id
        resolution.result_json = result
        resolution.updated_at = utc_now()
    if current is not None:
        if current.generation == generation:
            session.delete(current)
        else:
            current.resolution_ids_json = sorted(
                value
                for value in current.resolution_ids_json
                if value not in resolution_ids
            )
    session.commit()
    return True


# A deferred render that cannot be dispatched is tried again after a wait that grows with the
# age of its row (half its age, between these bounds), so a row that keeps failing is neither
# retried in a tight loop nor dropped.
DISPATCH_RETRY_SECONDS = 15.0
DISPATCH_RETRY_MAXIMUM_SECONDS = 600.0


def dispatch_due_proposal_render_or_postpone(session: Session, settings: Settings) -> bool:
    """``dispatch_due_proposal_render`` for the build worker's loop: a failure postpones the render.

    Until 14 November 2026 an exception here ended the worker process. A render check that ran
    while the build inputs it names were still arriving raised "requested build input snapshot
    does not exist", and every build of the Library waited until the service was started again.
    Now the failure is logged, the row stays and becomes due again later, and False is returned:
    nothing was dispatched, and the loop goes on with its builds.
    """

    try:
        return dispatch_due_proposal_render(session, settings)
    except Exception:  # noqa: BLE001 - one render must never stop the build worker
        session.rollback()
        logging.getLogger(__name__).exception(
            "a deferred proposal render could not be dispatched; it is tried again later"
        )
    try:
        now = utc_now()
        row = session.scalar(
            select(DeferredProposalRender)
            .where(DeferredProposalRender.ready_at <= now)
            .order_by(DeferredProposalRender.ready_at)
            .limit(1)
        )
        if row is not None:
            created = row.created_at or now
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = max(0.0, (now - created).total_seconds())
            wait = min(DISPATCH_RETRY_MAXIMUM_SECONDS, max(DISPATCH_RETRY_SECONDS, age / 2))
            row.ready_at = now + timedelta(seconds=wait)
            session.commit()
    except Exception:  # noqa: BLE001 - the next pass of the loop tries again
        session.rollback()
        logging.getLogger(__name__).exception("a failing deferred proposal render could not be postponed")
    return False
