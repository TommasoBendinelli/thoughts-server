# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from .models import ObjectRecord


class ObjectIntegrityError(ValueError):
    pass


class ObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._verified = OrderedDict()
        self._verification_lock = threading.RLock()

    @staticmethod
    def validate_sha256(value: str) -> str:
        normalized = value.casefold()
        if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
            raise ObjectIntegrityError("invalid SHA-256")
        return normalized

    def relative_path(self, sha256: str) -> Path:
        digest = self.validate_sha256(sha256)
        return Path(digest[:2]) / digest[2:4] / digest

    def path(self, sha256: str) -> Path:
        return self.root / self.relative_path(sha256)

    def matches(self, sha256: str, expected_size: int) -> bool:
        digest = self.validate_sha256(sha256)
        path = self.path(digest)
        def identity():
            stat = path.stat()
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        try:
            before = identity()
            if before[2] != expected_size:
                return False
            with self._verification_lock:
                if self._verified.get(digest) == before:
                    self._verified.move_to_end(digest)
                    return True
            if not self._matches(path, digest, expected_size) or identity() != before:
                return False
            with self._verification_lock:
                self._verified[digest] = before
                self._verified.move_to_end(digest)
                while len(self._verified) > 4096:
                    self._verified.popitem(last=False)
            return True
        except OSError:
            return False

    @staticmethod
    def _matches(path: Path, expected_sha256: str, expected_size: int) -> bool:
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            return False
        return size == expected_size and digest.hexdigest() == expected_sha256

    def stage(self) -> StagedObject:
        """Start receiving one object's bytes (see ``StagedObject``)."""
        return StagedObject(self)

    def put_stream(
        self,
        session: Session,
        stream: BinaryIO,
        media_type: str,
        expected_sha256: str | None = None,
    ) -> ObjectRecord:
        expected = self.validate_sha256(expected_sha256) if expected_sha256 else None
        staged = self.stage()
        try:
            while chunk := stream.read(1024 * 1024):
                staged.write(chunk)
            return staged.commit(session, media_type, expected)
        finally:
            staged.discard()

    def put_bytes(
        self,
        session: Session,
        content: bytes,
        media_type: str,
        expected_sha256: str | None = None,
    ) -> ObjectRecord:
        from io import BytesIO

        return self.put_stream(session, BytesIO(content), media_type, expected_sha256)

    def ingest_file(self, session: Session, path: Path, media_type: str) -> ObjectRecord:
        with path.open("rb") as stream:
            return self.put_stream(session, stream, media_type)

    def open(self, sha256: str) -> BinaryIO:
        return self.path(sha256).open("rb")

    def verify(self, session: Session) -> Iterator[tuple[str, bool, str]]:
        for record in session.query(ObjectRecord).order_by(ObjectRecord.sha256):
            path = self.path(record.sha256)
            if not path.is_file():
                yield record.sha256, False, "missing"
                continue
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            if digest.hexdigest() != record.sha256 or size != record.size_bytes:
                yield record.sha256, False, "digest-or-size-mismatch"
            else:
                yield record.sha256, True, "ok"


class StagedObject:
    """One object's bytes while they arrive: hashed and written to the staging folder chunk by chunk.

    ``write`` and ``commit`` block on the disk, so a caller on an event loop runs
    them in a worker thread. ``discard`` removes the staging file unless ``commit``
    moved it into the store; call it in a ``finally``.
    """

    def __init__(self, store: ObjectStore):
        self._store = store
        self._digest = hashlib.sha256()
        self.size = 0
        staging = store.root / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        descriptor, self._temporary_name = tempfile.mkstemp(prefix="object-", dir=staging)
        self._output: BinaryIO | None = os.fdopen(descriptor, "wb")

    @property
    def sha256(self) -> str:
        """The SHA-256 of the bytes written so far."""
        return self._digest.hexdigest()

    def write(self, chunk: bytes) -> None:
        assert self._output is not None
        self._digest.update(chunk)
        self.size += len(chunk)
        self._output.write(chunk)

    def commit(self, session: Session, media_type: str, expected_sha256: str | None = None) -> ObjectRecord:
        store = self._store
        expected = store.validate_sha256(expected_sha256) if expected_sha256 else None
        assert self._output is not None
        self._output.flush()
        os.fsync(self._output.fileno())
        self._output.close()
        self._output = None
        actual = self._digest.hexdigest()
        size = self.size
        if expected is not None and actual != expected:
            raise ObjectIntegrityError(f"object digest mismatch: expected {expected}, got {actual}")
        destination = store.path(actual)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and store._matches(destination, actual, size):
            Path(self._temporary_name).unlink()
        else:
            os.replace(self._temporary_name, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        record = session.get(ObjectRecord, actual)
        if record is None:
            record = ObjectRecord(
                sha256=actual,
                size_bytes=size,
                media_type=media_type or "application/octet-stream",
                relative_path=str(store.relative_path(actual)),
            )
            session.add(record)
            session.flush()
        elif record.size_bytes != size:
            raise ObjectIntegrityError("stored object metadata does not match content")
        return record

    def discard(self) -> None:
        if self._output is not None:
            with suppress(OSError):
                self._output.close()
            self._output = None
        with suppress(FileNotFoundError):
            Path(self._temporary_name).unlink()
