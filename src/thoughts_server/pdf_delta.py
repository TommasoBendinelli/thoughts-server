# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .object_store import ObjectStore

DELTA_FORMAT = "zstd-patch-v1"
DELTA_MEDIA_TYPE = "application/vnd.thoughts.zstd-patch"
MAX_DELTA_RATIO = 0.70


def create_pdf_delta(
    session: Session,
    store: ObjectStore,
    base_sha256: str,
    result_sha256: str,
    result_size_bytes: int,
) -> dict[str, str | int] | None:
    """Store a small zstd patch when one existing PDF can efficiently produce another."""
    if base_sha256 == result_sha256 or result_size_bytes <= 0:
        return None
    executable = shutil.which("zstd")
    base = store.path(base_sha256)
    result = store.path(result_sha256)
    if executable is None or not base.is_file() or not result.is_file():
        return None
    staging = store.root / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdf-delta-", dir=staging) as temporary_directory:
        patch = Path(temporary_directory) / "publication.zstpatch"
        try:
            completed = subprocess.run(
                [
                    executable,
                    "--quiet",
                    "--force",
                    f"--patch-from={base}",
                    str(result),
                    "-o",
                    str(patch),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0 or not patch.is_file():
            return None
        patch_size = patch.stat().st_size
        if patch_size <= 0 or patch_size >= result_size_bytes * MAX_DELTA_RATIO:
            return None
        record = store.ingest_file(session, patch, DELTA_MEDIA_TYPE)
    return {
        "format": DELTA_FORMAT,
        "base_sha256": base_sha256,
        "object_sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "result_sha256": result_sha256,
        "result_size_bytes": result_size_bytes,
    }
