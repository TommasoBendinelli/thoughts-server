# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Read-only source comparisons tied to immutable build inputs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath

from sqlalchemy import select

from .models import BuildInputSnapshot, BuildJob, Operation, Publication

TEXT_SUFFIXES = {".tex", ".md", ".markdown", ".sty", ".cls", ".bib", ".bst"}


def _text_path(path):
    p = PurePosixPath(path)
    return (
        bool(path)
        and not p.is_absolute()
        and ".." not in p.parts
        and "\\" not in path
        and p.suffix.lower() in TEXT_SUFFIXES
    )


def _read(store, digest):
    raw = store.path(digest).read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Saved source content failed its integrity check.")
    return raw


def _baseline(session, job, target):
    # Historical logs must never silently compare against a later publication.
    cutoff = job.started_at or job.created_at
    for previous in session.scalars(
        select(BuildJob)
        .where(
            BuildJob.completed_at <= cutoff,
            BuildJob.id != job.id,
        )
        .order_by(BuildJob.completed_at.desc())
    ):
        for outcome in (previous.result or {}).get("outcomes", []):
            if outcome.get("filename") == target and outcome.get("source_artifact_sha256"):
                return previous.id, outcome["source_artifact_sha256"]
    publication = session.get(Publication, target)
    if publication and publication.published_at <= cutoff and publication.source_artifact_sha256:
        return publication.build_id, publication.source_artifact_sha256
    raise ValueError("The source of the last successful PDF before this build is unavailable.")


def _inputs(session, job, target):
    if job is None:
        return {}
    digest = (
        (job.progress or {}).get("input_snapshot_sources", {}).get(target)
        if job.target == "all"
        else job.input_snapshot_sha256
    )
    snapshot = session.get(BuildInputSnapshot, digest) if digest else None
    return (
        {entry["path"]: entry["sha256"] for entry in snapshot.entries_json if _text_path(entry["path"])}
        if snapshot
        else {}
    )


def _sources(session, frontier):
    winners = {}
    for operation in session.scalars(
        select(Operation)
        .where(
            Operation.entity_type == "source",
            Operation.server_sequence <= frontier,
            Operation.outcome.in_(("applied", "merged")),
        )
        .order_by(Operation.server_sequence)
    ):
        winners[operation.entity_id] = operation.payload
    return winners


def build_source_diff(session, store, job):
    failures = (job.result or {}).get("failed_documents", [])
    targets = [
        (x["pdf_filename"], x.get("error", job.error or ""), job) for x in failures if x.get("pdf_filename")
    ]
    if not targets and job.target != "all":
        targets = [(job.target, job.error or "", job)]
    if not targets and job.target == "all":
        # A Sync can build another document successfully while still displaying
        # an earlier, unresolved failure. Follow the build which owns that error.
        seen = {x.get("filename") for x in (job.result or {}).get("outcomes", [])}
        known = set((job.progress or {}).get("input_snapshot_sources", {}))
        cutoff = job.started_at or job.created_at
        for previous in session.scalars(
            select(BuildJob)
            .where(
                BuildJob.completed_at <= cutoff,
                BuildJob.id != job.id,
            )
            .order_by(BuildJob.completed_at.desc())
        ):
            for outcome in (previous.result or {}).get("outcomes", []):
                seen.add(outcome.get("filename"))
            previous_failures = (previous.result or {}).get("failed_documents", [])
            if (
                not previous_failures
                and previous.target != "all"
                and previous.status in {"failed", "attention"}
            ):
                previous_failures = [{"pdf_filename": previous.target, "error": previous.error or ""}]
            for failure in previous_failures:
                target = failure.get("pdf_filename")
                if target and target not in seen and (not known or target in known):
                    targets.append((target, failure.get("error", previous.error or ""), previous))
                    seen.add(target)
        if not targets:
            targets = [
                (x["filename"], "", job) for x in (job.result or {}).get("outcomes", []) if x.get("filename")
            ]
    if not targets:
        raise ValueError("This build has no recorded document to compare.")
    documents = []
    for target, diagnostic, document_job in targets:
        winners = _sources(session, document_job.source_frontier)
        document = {"target": target, "build_id": document_job.id, "diagnostic": diagnostic, "files": []}
        documents.append(document)
        try:
            baseline_id, artifact_id = _baseline(session, document_job, target)
            artifact = json.loads(_read(store, artifact_id))
            if artifact.get("filename") != target:
                raise ValueError("The saved source artifact belongs to a different document.")
            before = {
                x["path"]: x.get("object", x.get("sha256"))
                for x in artifact["sources"]
                if _text_path(x["path"])
            }
            before.update(_inputs(session, session.get(BuildJob, baseline_id), target))
            # Include new project sources as well as dependencies in the old PDF.
            publication = session.get(Publication, target)
            project = publication.project if publication else ""
            baseline_job = session.get(BuildJob, baseline_id)
            if baseline_job:
                for path, payload in _sources(session, baseline_job.source_frontier).items():
                    if (
                        _text_path(path)
                        and project
                        and path.startswith(project + "/")
                        and not payload.get("deleted")
                    ):
                        before.setdefault(path, payload["result_object_sha256"])
            locations = re.findall(r"([^\s:]+\.(?:tex|md|markdown)):(\d+)", diagnostic)
            focus = locations[0][0] if locations else ""
            focus_line = int(locations[0][1]) if locations else 1
            after = {}
            for path, payload in winners.items():
                if (
                    not payload.get("deleted")
                    and _text_path(path)
                    and (path in before or path == focus or (project and path.startswith(project + "/")))
                ):
                    after[path] = payload["result_object_sha256"]
            after.update(_inputs(session, document_job, target))
            document.update(
                baseline_build_id=baseline_id,
                baseline_artifact_sha256=artifact_id,
                source_frontier=document_job.source_frontier,
                focus_path=focus,
                focus_line=focus_line,
            )
            for path in sorted(before.keys() | after.keys(), key=lambda p: (p != focus, p)):
                old, new = before.get(path), after.get(path)
                if old == new and path != focus:
                    continue
                left = _read(store, old) if old else b""
                right = _read(store, new) if new else b""
                if len(left) + len(right) > 8 * 1024 * 1024:
                    raise ValueError(f"Source file is too large to compare: {path}")
                document["files"].append(
                    {
                        "path": path,
                        "before": left.decode("utf-8"),
                        "after": right.decode("utf-8"),
                        "before_sha256": old,
                        "after_sha256": new,
                        "change": "added"
                        if old is None
                        else "deleted"
                        if new is None
                        else "unchanged"
                        if old == new
                        else "modified",
                        "line": focus_line if path == focus else 1,
                        "is_diagnostic": path == focus,
                    }
                )
        except (ValueError, OSError, KeyError) as error:
            document["files"] = []
            document["error"] = str(error)
    return {"schema_version": 1, "build_id": job.id, "documents": documents}
