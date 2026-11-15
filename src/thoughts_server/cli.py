# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import uvicorn
from sqlalchemy import func, select

from .agent_proposals import AgentProposalWorker
from .anchor_reconcile import reconcile_annotation_anchors
from .anchoring import AnnotationAnchorWorker
from .api import create_app
from .auth import create_device, generate_token, token_hash
from .build_inputs import migrate_legacy_build_input_snapshots
from .builds import BuildWorker, backfill_document_source_artifacts
from .config import MINIMUM_TOKEN_LENGTH, Settings
from .database import Database
from .library import reconcile_library
from .logical_comment_migration import migrate_logical_comments
from .migration import WorkspaceImporter, export_workspace
from .models import (
    BuildJob,
    Device,
    Operation,
    Publication,
    SourceFile,
    SourcePublicationRecord,
    TraceBuildLink,
    TraceEvent,
    TraceStream,
)
from .object_store import ObjectStore
from .pdf_retention import retire_superseded_pdfs
from .proposal_renders import dispatch_due_proposal_render_or_postpone
from .review_correspondence_migration import (
    migrate_review_correspondence,
    validate_review_correspondence_pairs,
    write_migration_report,
)
from .review_reanchor import (
    ReviewReanchorWorker,
    backfill_pdf_review_components,
    backfill_review_page_identities,
    backfill_review_reanchors,
    recover_interrupted_review_reanchors,
)
from .source_review_comments import refresh_document_review_comments
from .trace import valid_trace_id

# Caddy (public HTTPS) and Tailscale Serve both connect from loopback and set
# X-Forwarded-For.  Trust forwarded headers from nowhere else, and pass the
# value explicitly so uvicorn's FORWARDED_ALLOW_IPS environment default cannot
# widen it.
TRUSTED_PROXY_ADDRESSES = "127.0.0.1"
# At a stop, requests still working this long after the waiting ones were ended (an
# upload that is still arriving, say) are cancelled, so the API stops in seconds and
# never runs into systemd's stop timeout (90 s, then SIGKILL).
GRACEFUL_SHUTDOWN_SECONDS = 10


def server_options(settings: Settings) -> dict:
    return {
        "host": settings.api_host,
        "port": settings.api_port,
        "proxy_headers": True,
        "forwarded_allow_ips": TRUSTED_PROXY_ADDRESSES,
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS,
    }


class StoppingServer(uvicorn.Server):
    """uvicorn's server, which ends the requests that wait as soon as it is told to stop.

    uvicorn stops accepting connections, closes the idle ones and then waits for every
    open one to answer. A long poll answers when its wait is over (up to 60 s), and the
    trace stream of a build whose worker was stopped first never did, so a deploy waited
    for systemd's stop timeout. The waiting requests are told here
    (request_waits.HeldRequests) and answer at their next wake-up, which comes after the
    first ``await`` of uvicorn's shutdown, when the connections are already marked to
    close after their answer.
    """

    async def shutdown(self, sockets=None) -> None:
        from .request_waits import stop_held_requests

        stop_held_requests(self.config.app)
        await super().shutdown(sockets=sockets)


def run_server(app, options: dict) -> None:
    """``uvicorn.run`` for one process, with ``StoppingServer``."""
    server = StoppingServer(uvicorn.Config(app, **options))
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    if not server.started:
        # As uvicorn.run: the application did not start.
        raise SystemExit(3)


def server_main() -> None:
    from .multi_library import create_server_app

    # The API serves every Library; THOUGHTS_LIBRARY only selects one for workers and tools.
    settings = Settings.base_from_env()
    run_server(create_server_app(settings), server_options(settings))


def _explicit_token(token: str | None) -> str | None:
    if token is not None and len(token) < MINIMUM_TOKEN_LENGTH:
        raise SystemExit(f"--token must contain at least {MINIMUM_TOKEN_LENGTH} characters")
    return token


def worker_main() -> None:
    from .library_supervisor import supervise

    supervise(_worker_main)


def _worker_main() -> None:
    # Build start, stage timings and outcomes reach the journal; Python drops INFO without a handler.
    sync_log = logging.getLogger("thoughts.sync")
    if not sync_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        sync_log.addHandler(handler)
        sync_log.setLevel(logging.INFO)
    settings = Settings.from_env()
    settings.ensure_directories()
    database = settings.worker_database()
    database.create_schema()
    store = ObjectStore(settings.object_root)
    proposal_worker = AgentProposalWorker(settings, database.session_factory, store)
    build_worker = BuildWorker(settings, database.session_factory, store)
    proposal_worker.recover_running_jobs()
    build_worker.recover_running_jobs()
    from .html_edition.jobs import HtmlEditionWorker

    # HTML editions convert beside the build loop, so they never delay a build or Sync.
    HtmlEditionWorker(settings, database.session_factory, store).start_background()
    if settings.workers_on_demand:
        from .on_demand_workers import OnDemandWorkers

        # The export, anchor and review workers of this Library run only while they have work.
        OnDemandWorkers(settings, database.session_factory).start_background()
    next_figure_collection = 0.0
    idle = WorkerIdleness(settings)
    while True:
        if time.monotonic() >= next_figure_collection:
            # Hourly: delete figures of rejected proposals that no note uses any more.
            next_figure_collection = time.monotonic() + 3600
            try:
                from .agent_figures import collect_orphans
                collect_orphans(database.session_factory, store, settings.worktree_root)
            except Exception:  # noqa: BLE001 - cleanup must never stop the build worker
                logging.getLogger(__name__).exception("agent figure cleanup failed")
        proposal_work = proposal_worker.run_once()
        with database.session_factory() as session:
            deferred_render_work = dispatch_due_proposal_render_or_postpone(session, settings)
            renders_waiting = deferred_render_work or pending_proposal_renders(session)
        build_work = build_worker.run_once()
        if not proposal_work and not deferred_render_work and not build_work:
            build_worker.wait_for_work(idle.wait_seconds(renders_waiting))
        else:
            idle.worked()


class WorkerIdleness:
    """How long the build worker's loop may wait once it has had nothing to do for a while.

    A new build wakes the loop by notification, whatever the wait. Two things it
    also serves have no notification: a proposal render that becomes due at a set
    time, so the loop keeps its short poll while one is waiting, and the recovery of
    a proposal resolution the API did not finish, which the longer wait delays by at
    most ``worker_idle_poll_seconds``.
    """

    RELAX_AFTER_SECONDS = 30.0

    def __init__(self, settings: Settings, clock=time.monotonic):
        self.settings = settings
        self.clock = clock
        self.idle_since: float | None = None

    def worked(self) -> None:
        self.idle_since = None

    def wait_seconds(self, renders_waiting: bool) -> float | None:
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
        if renders_waiting or now - self.idle_since < self.RELAX_AFTER_SECONDS:
            return None
        return self.settings.worker_idle_poll_seconds


def pending_proposal_renders(session) -> bool:
    from .models import DeferredProposalRender

    return session.scalar(select(DeferredProposalRender.target).limit(1)) is not None


def _waits_for_the_build_worker(kind: str) -> bool:
    """With on-demand workers the stand-alone service of a low-volume worker only waits.

    The Library's build worker starts that worker while it has work; a second one
    from this service would only add an idle process per Library (and two export
    workers refuse to run side by side).
    """
    from .on_demand_workers import IdleExit

    if IdleExit.from_environment() is not None or not Settings.base_from_env().workers_on_demand:
        return False
    logging.getLogger(__name__).warning(
        "THOUGHTS_WORKERS_ON_DEMAND is set: the %s worker is started by each Library's build worker "
        "while it has work; this service only waits and can be disabled", kind,
    )
    while True:
        time.sleep(3600)


def export_worker_main() -> None:
    from .library_supervisor import supervise

    if not _waits_for_the_build_worker("export"):
        supervise(_export_worker_main)


def _export_worker_main() -> None:
    from .on_demand_workers import IdleExit
    from .project_exports import ProjectExportWorker
    settings = Settings.from_env()
    settings.ensure_directories()
    database = settings.worker_database()
    database.create_schema()
    ProjectExportWorker(database.session_factory, ObjectStore(settings.object_root),
                        settings.state_root / "project-exports").run_forever(IdleExit.from_environment())


def anchor_worker_main() -> None:
    from .library_supervisor import supervise

    if not _waits_for_the_build_worker("anchor"):
        supervise(_anchor_worker_main)


def _anchor_worker_main() -> None:
    from .on_demand_workers import IdleExit
    settings = Settings.from_env()
    settings.ensure_directories()
    database = settings.worker_database()
    database.create_schema()
    AnnotationAnchorWorker(
        settings,
        database.session_factory,
        ObjectStore(settings.object_root),
    ).run_forever(IdleExit.from_environment())


def review_worker_main() -> None:
    from .library_supervisor import supervise

    if not _waits_for_the_build_worker("review"):
        supervise(_review_worker_main)


def _review_worker_main() -> None:
    from .on_demand_workers import IdleExit
    settings = Settings.from_env()
    settings.ensure_directories()
    database = settings.worker_database()
    database.create_schema()
    ReviewReanchorWorker(
        settings,
        database.session_factory,
        ObjectStore(settings.object_root),
    ).run_forever(IdleExit.from_environment())


def _list_libraries() -> None:
    from .libraries import all_library_settings, library_names

    names = library_names()
    for settings in all_library_settings(Settings.base_from_env()):
        database = Database(settings.database_url)
        try:
            with database.session_factory() as session:
                devices = session.scalar(select(func.count()).select_from(Device).where(Device.enabled.is_(True)))
        except Exception:  # noqa: BLE001 - a Library without a schema yet has no devices
            devices = 0
        finally:
            database.close()
        root = settings.state_root.parent if settings.library_id in names else settings.state_root
        print(f"{settings.library_id}\t{names.get(settings.library_id, 'Default Library')}\t{devices} devices\t{root}")


def _record_default_folder_segment(database: Database) -> None:
    """A new, empty Library starts with ``default`` folders, like a migrated one."""
    from . import default_folder_segment as segments
    from .models import Entity, Operation, SourceFile

    with database.session_factory() as session:
        if segments.stored(session) == segments.CANONICAL:
            print("default_folder_segment=default")
            return
        if any(session.scalar(select(func.count()).select_from(model)) for model in (Operation, Entity, SourceFile)):
            raise SystemExit("This Library has content; migrate it with thoughts_server.migrate_default_folder")
        segments.store(session, segments.CANONICAL)
        session.commit()
    print("default_folder_segment=default")


def _upgrade_library_databases(alembic_config: str) -> None:
    """``alembic upgrade head`` for every registry Library (the default Library's runs separately)."""
    import os
    import sys

    from .libraries import all_library_settings

    for settings in all_library_settings(Settings.base_from_env())[1:]:
        settings.ensure_directories()
        environment = dict(os.environ, THOUGHTS_DATABASE_URL=settings.database_url)
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", alembic_config, "upgrade", "head"],
            env=environment, check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"Migrating the database of Library {settings.library_id!r} failed")
        print(f"Library {settings.library_id}: database at the latest schema.")


def admin_main() -> None:
    parser = argparse.ArgumentParser(description="Administer Thoughts Server")
    parser.add_argument("--database-url")
    parser.add_argument("--library", help="the Library to administer (default: the default Library)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db")
    subparsers.add_parser("list-libraries")
    subparsers.add_parser("record-default-folder-segment")
    upgrade_libraries_parser = subparsers.add_parser("upgrade-library-databases")
    upgrade_libraries_parser.add_argument("--alembic-config", default="alembic.ini")
    create_parser = subparsers.add_parser("create-device")
    create_parser.add_argument("device_id")
    create_parser.add_argument("--name")
    create_parser.add_argument("--token")
    revoke_parser = subparsers.add_parser("revoke-device")
    revoke_parser.add_argument("device_id")
    rotate_parser = subparsers.add_parser("rotate-device")
    rotate_parser.add_argument("device_id")
    rotate_parser.add_argument("--token")
    subparsers.add_parser("list-devices")
    from .auto_deleted_comments import LIMITATION
    report_parser = subparsers.add_parser(
        "report-auto-deleted-comments",
        help="List comments deleted automatically because their source marker vanished (read-only).",
        description=(
            "Read-only dry run: list comments that earlier releases deleted automatically because "
            "their source anchor disappeared (server canonical_source_marker_removed, desktop "
            "source_anchor_removed). Nothing is restored or written. Limitation: " + LIMITATION
        ),
    )
    report_parser.add_argument("--format", choices=("json", "tsv"), default="json")
    report_parser.add_argument("--docs-root", type=Path,
                               help="Also read comments/review_resolutions.jsonl and comments/*_comments.jsonl here.")
    subparsers.add_parser("verify-objects")
    pdf_retention_parser = subparsers.add_parser("retire-generated-pdfs")
    pdf_retention_parser.add_argument("--commit", action="store_true")
    pdf_retention_parser.add_argument("--min-age-hours", type=float, default=24.0)
    identity_parser = subparsers.add_parser("migrate-document-identities")
    identity_parser.add_argument("--commit", action="store_true")
    identity_parser.add_argument("--receipt", type=Path)
    subparsers.add_parser("backfill-document-source-artifacts")
    subparsers.add_parser("refresh-source-review-comments")
    subparsers.add_parser("backfill-proposal-history")
    wrapper_backfill_parser = subparsers.add_parser("backfill-proposal-wrappers")
    wrapper_backfill_parser.add_argument("--commit", action="store_true")
    purge_history_parser = subparsers.add_parser("purge-proposal-history")
    purge_history_parser.add_argument("--commit", action="store_true")
    purge_history_parser.add_argument("--backup", type=Path)
    build_input_migration_parser = subparsers.add_parser("migrate-build-input-snapshots")
    build_input_migration_parser.add_argument("--device-id", required=True)
    build_input_migration_parser.add_argument("--commit", action="store_true")
    review_backfill_parser = subparsers.add_parser("backfill-review-reanchors")
    review_backfill_parser.add_argument("--force", action="store_true")
    subparsers.add_parser("backfill-pdf-review-components")
    page_identity_parser = subparsers.add_parser(
        "backfill-review-page-identities",
        help="List placed source-linked cards without a page_visual_sha256; with --commit, place them again.",
    )
    page_identity_parser.add_argument("--commit", action="store_true")
    subparsers.add_parser("recover-review-reanchors")
    linked_repair_parser = subparsers.add_parser(
        "plan-linked-card-repairs",
        help="Dry run: show the source markers the review worker would insert for cards without a "
             "source link on documents with an editable source. Writes nothing.",
    )
    linked_repair_parser.add_argument("--include-attempted", action="store_true")
    trace_parser = subparsers.add_parser("inspect-trace")
    trace_parser.add_argument("trace_id")
    args = parser.parse_args()
    if args.library:
        import os

        from .libraries import library_environment, library_settings

        # Administer the Library as if it were the server's only one, renderer environment included.
        selected = library_settings(Settings.base_from_env(), args.library)
        environment = library_environment(selected)
        os.environ.clear()
        os.environ.update(environment)
    if args.command == "list-libraries":
        _list_libraries()
        return
    if args.command == "upgrade-library-databases":
        _upgrade_library_databases(args.alembic_config)
        return
    settings = Settings.from_env()
    if args.command == "retire-generated-pdfs" and args.commit:
        services = (
            "thoughts-api.service", "thoughts-worker.service",
            "thoughts-anchor-worker.service", "thoughts-review-worker.service",
            "thoughts-source-history.service",
            "thoughts-source-history.timer", "thoughts-backup.timer", "thoughts-integrity.timer",
        )
        active = [service for service in services if subprocess.run(
            ["systemctl", "is-active", "--quiet", service], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0]
        if active:
            raise SystemExit(
                "Stop all Thoughts API and worker services before PDF retirement: " + ", ".join(active)
            )
    database = Database(args.database_url or settings.database_url)
    if args.command == "report-auto-deleted-comments":
        # A report never writes, not even missing tables.
        from .auto_deleted_comments import auto_deleted_comment_report, format_report
        with database.session_factory() as session:
            rows = auto_deleted_comment_report(session, args.docs_root)
            session.rollback()
        print(format_report(rows, args.format))
        return
    if args.command == "plan-linked-card-repairs":
        from .review_source_link import plan_repairs
        with database.session_factory() as session:
            report = plan_repairs(session, ObjectStore(settings.object_root),
                                  include_queued=args.include_attempted)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    if args.command == "record-default-folder-segment":
        _record_default_folder_segment(database)
        return
    database.create_schema()
    if args.command == "init-db":
        print("Database schema initialized.")
        return
    with database.session_factory() as session:
        if args.command == "retire-generated-pdfs":
            if args.min_age_hours < 0:
                raise SystemExit("--min-age-hours must be nonnegative")
            report = retire_superseded_pdfs(
                session, ObjectStore(settings.object_root), settings.state_root,
                commit=args.commit, min_age=timedelta(hours=args.min_age_hours),
            )
            print(json.dumps(report, sort_keys=True))
        elif args.command == "create-device":
            token = create_device(
                session, args.device_id, args.name or args.device_id, _explicit_token(args.token)
            )
            print(json.dumps({"device_id": args.device_id, "token": token}))
        elif args.command == "list-devices":
            for device in session.scalars(select(Device).order_by(Device.id)):
                print(json.dumps({"id": device.id, "name": device.name, "enabled": device.enabled}))
        elif args.command == "revoke-device":
            device = session.get(Device, args.device_id)
            if device is None:
                raise SystemExit(f"Unknown device: {args.device_id}")
            device.enabled = False
            session.commit()
            print(json.dumps({"device_id": device.id, "enabled": False}))
        elif args.command == "rotate-device":
            device = session.get(Device, args.device_id)
            if device is None:
                raise SystemExit(f"Unknown device: {args.device_id}")
            plain_token = _explicit_token(args.token) or generate_token()
            device.token_hash = token_hash(plain_token)
            device.enabled = True
            session.commit()
            print(json.dumps({"device_id": device.id, "token": plain_token}))
        elif args.command == "migrate-document-identities":
            from .document_identity_migration import migrate_document_identities
            result = migrate_document_identities(session, ObjectStore(settings.object_root), settings.docs_root)
            if args.commit:
                session.commit()
            else:
                session.rollback()
            if args.commit and settings.docs_root:
                from .sources import _materialize_canonical_path
                for path in result["source_manifests"]:
                    _materialize_canonical_path(session, ObjectStore(settings.object_root), settings.docs_root, path)
                from .library import write_library_index
                write_library_index(session, settings.docs_root)
            result["committed"] = args.commit
            if args.receipt:
                args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "verify-objects":
            failures = 0
            for sha256, ok, detail in ObjectStore(settings.object_root).verify(session):
                print(f"{sha256}\t{'ok' if ok else 'failed'}\t{detail}")
                failures += int(not ok)
            if failures:
                raise SystemExit(2)
        elif args.command == "backfill-document-source-artifacts":
            print(
                json.dumps(
                    backfill_document_source_artifacts(
                        settings,
                        database.session_factory,
                        ObjectStore(settings.object_root),
                    )
                )
            )
        elif args.command == "purge-proposal-history":
            from .proposal_history import purge_history
            from .sources import source_transaction_lock, worktree_lock
            if args.commit and args.backup is None:
                raise SystemExit("--commit requires --backup PATH")
            with worktree_lock(settings.worktree_root), source_transaction_lock(session, "proposal-history-purge"):
                summary = purge_history(session, args.backup, commit=args.commit)
                if args.commit:
                    session.commit()
            print(json.dumps({**summary, "committed": args.commit}))
        elif args.command == "backfill-proposal-history":
            from .proposal_history import reconcile_proposal_history
            from .sources import source_transaction_lock, worktree_lock
            with worktree_lock(settings.worktree_root), source_transaction_lock(session, "proposal-history-backfill"):
                reconcile_proposal_history(session, ObjectStore(settings.object_root), {},
                                           source_operation_id="proposal-history-backfill-v1")
                session.commit()
            print(json.dumps({"proposal_history": "observed current canonical sources"}))
        elif args.command == "backfill-proposal-wrappers":
            from .proposal_history import backfill_wrapper_hashes
            from .sources import source_transaction_lock, worktree_lock
            with worktree_lock(settings.worktree_root), source_transaction_lock(session, "proposal-wrapper-backfill"):
                summary = backfill_wrapper_hashes(session, ObjectStore(settings.object_root), commit=args.commit)
            print(json.dumps(summary, sort_keys=True))
        elif args.command == "refresh-source-review-comments":
            paths = set(session.scalars(select(SourceFile.path).where(SourceFile.deleted.is_(False))))
            refreshed = refresh_document_review_comments(
                settings,
                session,
                ObjectStore(settings.object_root),
                paths,
            )
            session.commit()
            print(json.dumps({"refreshed": refreshed}, sort_keys=True))
        elif args.command == "migrate-build-input-snapshots":
            print(
                json.dumps(
                    migrate_legacy_build_input_snapshots(
                        session,
                        ObjectStore(settings.object_root),
                        args.device_id,
                        commit=args.commit,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "backfill-review-reanchors":
            queued = backfill_review_reanchors(session, force=args.force)
            session.commit()
            print(json.dumps({"queued": queued}))
        elif args.command == "backfill-pdf-review-components":
            result = backfill_pdf_review_components(session, ObjectStore(settings.object_root))
            session.commit()
            print(json.dumps(result, sort_keys=True))
        elif args.command == "backfill-review-page-identities":
            result = backfill_review_page_identities(session, ObjectStore(settings.object_root), commit=args.commit)
            if args.commit:
                session.commit()
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "recover-review-reanchors":
            recovered = recover_interrupted_review_reanchors(session)
            session.commit()
            print(json.dumps({"recovered": recovered}))
        elif args.command == "inspect-trace":
            trace_id = valid_trace_id(args.trace_id)
            if trace_id is None:
                raise SystemExit("Trace ID must be a UUID")
            stream = session.get(TraceStream, trace_id)
            linked_build_ids = list(
                session.scalars(
                    select(TraceBuildLink.build_id)
                    .where(TraceBuildLink.trace_id == trace_id)
                    .order_by(TraceBuildLink.linked_at)
                )
            )
            records = {
                "trace_id": trace_id,
                "stream": (
                    {
                        "owner_device_id": stream.owner_device_id,
                        "target": stream.target,
                        "last_sequence": stream.last_sequence,
                        "server_state": stream.server_state,
                        "terminal": stream.terminal,
                        "created_at": stream.created_at.isoformat(),
                        "updated_at": stream.updated_at.isoformat(),
                        "terminal_at": stream.terminal_at.isoformat() if stream.terminal_at else None,
                    }
                    if stream is not None
                    else None
                ),
                "events": [
                    {
                        "sequence": row.sequence,
                        "event_key": row.event_key,
                        "timestamp": row.occurred_at.isoformat(),
                        "component": row.component,
                        "phase": row.phase,
                        "state": row.state,
                        "target": row.target,
                        "build_id": row.build_id,
                        "message": row.message,
                        "completed": row.completed,
                        "total": row.total,
                        "bytes_completed": row.bytes_completed,
                        "bytes_total": row.bytes_total,
                        "error_code": row.error_code,
                        "terminal": row.terminal,
                    }
                    for row in session.scalars(
                        select(TraceEvent)
                        .where(TraceEvent.trace_id == trace_id)
                        .order_by(TraceEvent.sequence)
                    )
                ],
                "operations": [
                    {
                        "operation_id": row.operation_id,
                        "device": row.device_id,
                        "target": row.collection,
                        "phase": "uploading",
                        "status": row.outcome,
                        "server_sequence": row.server_sequence,
                    }
                    for row in session.scalars(
                        select(Operation)
                        .where(Operation.trace_id == trace_id)
                        .order_by(Operation.server_sequence)
                    )
                ],
                "source_publications": [
                    {
                        "id": row.id,
                        "device": row.device_id,
                        "source_frontier": row.source_frontier,
                        "status": "succeeded",
                    }
                    for row in session.scalars(
                        select(SourcePublicationRecord)
                        .where(SourcePublicationRecord.trace_id == trace_id)
                        .order_by(SourcePublicationRecord.created_at)
                    )
                ],
                "builds": [
                    {
                        "id": row.id,
                        "device": row.requested_by,
                        "target": row.target,
                        "phase": (row.progress or {}).get("phase"),
                        "status": row.status,
                        "attempts": row.attempts,
                    }
                    for row in session.scalars(
                        select(BuildJob)
                        .where(
                            (BuildJob.trace_id == trace_id)
                            | (BuildJob.id.in_(linked_build_ids) if linked_build_ids else False)
                        )
                        .order_by(BuildJob.created_at)
                    )
                ],
                "publications": [
                    {
                        "filename": row.filename,
                        "build_id": row.build_id,
                        "source_frontier": row.source_frontier,
                        "sha256": row.object_sha256,
                    }
                    for row in session.scalars(
                        select(Publication)
                        .where(Publication.trace_id == trace_id)
                        .order_by(Publication.filename)
                    )
                ],
            }
            print(json.dumps(records, indent=2, sort_keys=True))


def migrate_main() -> None:
    parser = argparse.ArgumentParser(description="Import or export Thoughts workspace data")
    parser.add_argument("--database-url")
    parser.add_argument("--object-root", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--docs-root", required=True, type=Path)
    import_parser.add_argument("--commit", action="store_true")
    reconcile_parser = subparsers.add_parser("reconcile-library")
    reconcile_parser.add_argument("--docs-root", required=True, type=Path)
    reconcile_parser.add_argument("--commit", action="store_true")
    anchor_reconcile_parser = subparsers.add_parser("reconcile-anchors")
    anchor_reconcile_parser.add_argument("--docs-root", required=True, type=Path)
    anchor_reconcile_parser.add_argument("--commit", action="store_true")
    logical_comment_parser = subparsers.add_parser("migrate-logical-comments")
    logical_comment_parser.add_argument("--docs-root", required=True, type=Path)
    logical_comment_parser.add_argument("--commit", action="store_true")
    review_correspondence_parser = subparsers.add_parser("migrate-review-correspondence")
    review_correspondence_parser.add_argument("--docs-root", required=True, type=Path)
    review_correspondence_parser.add_argument("--commit", action="store_true")
    review_correspondence_parser.add_argument("--report", type=Path)
    review_correspondence_parser.add_argument("--validate-pairs", action="store_true")
    review_correspondence_parser.add_argument("--write-pair-approvals", action="store_true")
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    database = Database(args.database_url or settings.database_url)
    database.create_schema()
    store = ObjectStore(args.object_root or settings.object_root)
    with database.session_factory() as session:
        if args.command == "import":
            report = WorkspaceImporter(args.docs_root, session, store).run(commit=args.commit)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
            if report.errors:
                raise SystemExit(2)
        elif args.command == "reconcile-library":
            report = reconcile_library(args.docs_root, session, store, commit=args.commit)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
            if report.errors:
                raise SystemExit(2)
        elif args.command == "reconcile-anchors":
            report = reconcile_annotation_anchors(args.docs_root, session, commit=args.commit)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
            if report.errors:
                raise SystemExit(2)
        elif args.command == "migrate-logical-comments":
            report = migrate_logical_comments(args.docs_root, session, commit=args.commit)
            if args.commit:
                export_workspace(session, args.docs_root, write_manifest=False, prune=False)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        elif args.command == "migrate-review-correspondence":
            if args.write_pair_approvals and not args.validate_pairs:
                parser.error("--write-pair-approvals requires --validate-pairs")
            if args.validate_pairs:
                pair_report = validate_review_correspondence_pairs(
                    args.docs_root,
                    write_approvals=args.write_pair_approvals,
                )
                print(json.dumps({"pair_validation": pair_report.as_dict()}, indent=2, sort_keys=True))
            report = migrate_review_correspondence(args.docs_root, commit=args.commit)
            if args.report is not None:
                write_migration_report(report, args.report)
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            print(json.dumps(export_workspace(session, args.destination), indent=2, sort_keys=True))
