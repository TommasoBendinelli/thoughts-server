# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The client's request records: one per request_id, so a retry never writes twice.

``requests.json`` in the state directory (default ``~/.local/state/thoughts-cli``)
maps ``<server>#<device token fingerprint>#<request_id>`` to a record:

- ``prepared``: the source was authored and its publication built, but the
  server's answer is not known yet. A retry sends the same publication again
  (same publication, operation IDs and timestamp); the server answers a
  publication it already applied from its own record.
- ``applied``: the publication was answered; a retry returns ``result`` with
  ``replayed: true``.

The authored bytes of a prepared request are kept beside it (``pending/``) so a
retry can upload them again. One process at a time holds ``requests.lock``
(fcntl), like the desktop's proposal_authoring.lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
APPLIED_RETENTION = timedelta(days=180)


def default_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "thoughts-cli"


def token_fingerprint(token: str) -> str:
    """Enough of the token's hash to keep two devices' records apart; never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class Busy(Exception):
    pass


class RequestStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "requests.json"
        self.pending = self.directory / "pending"

    def _ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self, timeout: float = 30.0) -> Iterator[RequestStore]:
        self._ensure()
        with open(self.directory / "requests.lock", "a+") as handle:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy("Another agent proposal operation is running.") from None
                    time.sleep(0.1)
            try:
                yield self
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # The methods below are called with the lock held.

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": SCHEMA_VERSION, "requests": {}}
        if not isinstance(data, dict) or not isinstance(data.get("requests"), dict):
            raise ValueError(f"{self.path} is not a thoughts-cli request file")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._ensure()
        cutoff = datetime.now(UTC) - APPLIED_RETENTION
        requests = data["requests"]
        for key in [key for key, record in requests.items()
                    if record.get("status") == "applied" and _older(record.get("updated_at"), cutoff)]:
            del requests[key]
        handle, temporary = tempfile.mkstemp(prefix=".requests-", dir=self.directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, sort_keys=True, indent=1)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> dict[str, Any] | None:
        return self._load()["requests"].get(key)

    def put(self, key: str, record: dict[str, Any]) -> None:
        data = self._load()
        record = {**record, "updated_at": datetime.now(UTC).isoformat()}
        record.setdefault("created_at", record["updated_at"])
        data["requests"][key] = record
        self._save(data)

    def delete(self, key: str) -> None:
        data = self._load()
        record = data["requests"].pop(key, None)
        if record is not None:
            self._save(data)
            self.drop_content(record.get("content_sha256"))

    # Authored bytes of prepared requests.

    def keep_content(self, digest: str, content: bytes) -> None:
        self.pending.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.pending / digest
        if not target.exists():
            temporary = self.pending / f".{digest}.tmp"
            temporary.write_bytes(content)
            os.replace(temporary, target)

    def content(self, digest: str | None) -> bytes | None:
        if not digest:
            return None
        try:
            data = (self.pending / digest).read_bytes()
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == digest else None

    def drop_content(self, digest: str | None) -> None:
        if not digest:
            return
        still_needed = any(record.get("content_sha256") == digest and record.get("status") == "prepared"
                           for record in self._load()["requests"].values())
        if not still_needed:
            (self.pending / digest).unlink(missing_ok=True)


def _older(value: Any, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(value) < cutoff
    except (TypeError, ValueError):
        return False
