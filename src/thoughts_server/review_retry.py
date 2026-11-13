# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Durable generation drafts and explicit retry of final validation/publication.

Run with the service environment: python -m thoughts_server.review_retry JOB_ID
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from .models import BuildJob, utc_now


def job_directory(settings, job_id: str) -> Path:
    directory = settings.state_root / "review-generation-jobs" / hashlib.sha256(job_id.encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def write_json(path: Path, value: dict) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
        staged = Path(temporary) / "data.json"
        with staged.open("w") as output:
            os.chmod(staged, 0o600)
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        staged.replace(path)


def save_drafts(settings, job_id: str, result: dict) -> None:
    write_json(job_directory(settings, job_id) / "drafts.json", {"job_id": job_id, "result": result})


def load_drafts(settings, job_id: str) -> dict | None:
    path = job_directory(settings, job_id) / "drafts.json"
    if not path.is_file():
        return None
    saved = json.loads(path.read_text())
    if saved.get("job_id") != job_id or not isinstance(
        saved.get("result", {}).get("review_generation"), dict
    ):
        raise ValueError("Saved review drafts do not belong to this job")
    return saved["result"]


def queue_retry(session, settings, job_id: str):
    job = session.scalar(select(BuildJob).where(BuildJob.id == job_id).with_for_update())
    if job is None or job.repair_mode != "review_cards":
        raise ValueError("Review generation job not found")
    if job.status in {"queued", "running"}:
        return job
    if job.status != "failed" or load_drafts(settings, job_id) is None:
        raise ValueError("Only failed generation jobs with saved drafts can retry final checking")
    job.status = "queued"
    job.error = None
    job.completed_at = None
    job.cancellation_requested = False
    job.updated_at = utc_now()
    job.result = {}
    job.progress = {
        **(job.progress or {}),
        "phase": "queued",
        "detail": "Saved drafts queued for duplicate checking and publication",
    }
    session.commit()
    return job


def main():
    import argparse

    from .config import Settings
    from .database import Database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id")
    args = parser.parse_args()
    settings = Settings.from_env()
    database = Database(settings.database_url)
    with database.session_factory() as session:
        job = queue_retry(session, settings, args.job_id)
        print(json.dumps({"job_id": job.id, "status": job.status, "detail": job.progress["detail"]}))


if __name__ == "__main__":
    main()
