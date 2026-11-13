# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Export committed source publications into a private, source-only Git history.

The database and object store remain authoritative. This disposable projection
never reads the mutable authoring worktree and never writes to the database.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select

from .config import Settings
from .database import Database
from .models import Operation, SourceFile, SourcePublicationRecord
from .object_store import ObjectStore
from .sources import worktree_lock

SUFFIXES = {".tex", ".bib", ".sty", ".cls", ".bst"}
EXCLUDED = {"build", "generated", "output", "outputs", "tmp", "__pycache__"}
HEAD = "refs/heads/main"
CURSOR = "refs/thoughts/source-frontier"


def eligible(path: str) -> bool:
    parts = PurePosixPath(path).parts
    # A Project's default folder is a hidden path; only its own contents are checked.
    from .source_owner import is_default_folder, split
    owned = split(path)
    checked = PurePosixPath(owned[1]).parts if owned and is_default_folder(owned[0]) else parts
    if (
        not parts
        or path.startswith("/")
        or "\\" in path
        or str(PurePosixPath(path)) != path
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(part.startswith(".") or part.lower() in EXCLUDED for part in checked)
    ):
        return False
    return PurePosixPath(path).suffix.lower() in SUFFIXES


@dataclass
class Checkpoint:
    frontier: int
    title: str
    detail: str
    timestamp: datetime
    files: dict[str, str | None]


def read_checkpoints(database: Database, worktree: Path, cursor: int | None) -> list[Checkpoint]:
    # Every canonical source writer holds this lock through its DB commit.
    # It also prevents a lower sequence being committed after our cursor advances.
    with worktree_lock(worktree), database.session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        accepted = (Operation.entity_type == "source", Operation.outcome.in_(("applied", "merged")))
        if cursor is None:
            frontier = int(session.scalar(select(func.max(Operation.server_sequence)).where(*accepted)) or 0)
            files = {
                row.path: row.object_sha256
                for row in session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False)))
                if eligible(row.path)
            }
            return [
                Checkpoint(
                    frontier,
                    "Initial Thoughts source checkpoint",
                    "Baseline of sources already saved on the server.",
                    datetime.now(UTC),
                    files,
                )
            ]
        operations = list(
            session.scalars(
                select(Operation)
                .where(
                    *accepted,
                    Operation.server_sequence > cursor,
                )
                .order_by(Operation.server_sequence)
            )
        )
        if not operations:
            return []
        publications = list(
            session.scalars(
                select(SourcePublicationRecord).where(
                    SourcePublicationRecord.source_frontier > cursor,
                )
            )
        )
        by_sequence = {}
        for publication in publications:
            for result in publication.results_json:
                sequence = result.get("server_sequence")
                if result.get("outcome") in {"applied", "merged"} and sequence and sequence > cursor:
                    if sequence in by_sequence:
                        raise RuntimeError("source operation belongs to multiple publications")
                    by_sequence[sequence] = publication
        checkpoints: list[Checkpoint] = []
        previous_key = None
        seen = set()
        for operation in operations:
            publication = by_sequence.get(operation.server_sequence)
            key = publication.id if publication else operation.operation_id
            if key != previous_key:
                if key in seen:
                    raise RuntimeError("source publication operations are interleaved")
                seen.add(key)
                previous_key = key
                kind = "publication" if publication else "operation"
                checkpoints.append(
                    Checkpoint(
                        operation.server_sequence,
                        f"Thoughts source {kind} {key}",
                        f"Device: {operation.device_id}\nSource-{kind}: {key}",
                        publication.created_at if publication else operation.accepted_at,
                        {},
                    )
                )
            checkpoint = checkpoints[-1]
            checkpoint.frontier = operation.server_sequence
            if eligible(operation.entity_id):
                checkpoint.files[operation.entity_id] = (
                    None if operation.payload.get("deleted") else operation.payload["result_object_sha256"]
                )
        return checkpoints


class History:
    def __init__(self, root: Path, store: ObjectStore):
        self.root = root.resolve()
        self.store = store
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Thoughts Sync",
            GIT_AUTHOR_EMAIL="thoughts@localhost",
            GIT_COMMITTER_NAME="Thoughts Sync",
            GIT_COMMITTER_EMAIL="thoughts@localhost",
        )

    def git(self, *args: str, data: bytes | None = None) -> bytes:
        result = subprocess.run(
            ["git", "--git-dir", str(self.root), *args],
            input=data,
            capture_output=True,
            env=self.env,
            timeout=120,
        )
        if result.returncode:
            raise RuntimeError(
                f"source history git {args[0]} failed: {result.stderr.decode(errors='replace')}"
            )
        return result.stdout

    def initialize(self) -> None:
        if not self.root.exists():
            self.root.mkdir(mode=0o700)
        if not (self.root / "HEAD").exists():
            if any(self.root.iterdir()):
                raise RuntimeError("source history destination is not an empty directory")
            self.git("init", "--bare", "--initial-branch=main", str(self.root))
            self.git("config", "gc.auto", "256")
            self.git("config", "gc.autoDetach", "false")
            self.git("config", "maintenance.autoDetach", "false")
        if self.git("rev-parse", "--is-bare-repository").strip() != b"true":
            raise RuntimeError("source history requires its own bare repository")

    def refs(self) -> dict[str, str]:
        return dict(
            line.split(" ", 1)
            for line in self.git(
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                HEAD,
                CURSOR,
            )
            .decode()
            .splitlines()
        )

    def cursor(self) -> int | None:
        refs = self.refs()
        if bool(refs.get(HEAD)) != bool(refs.get(CURSOR)):
            raise RuntimeError("source history checkpoint and cursor disagree")
        if CURSOR not in refs:
            return None
        return int(self.git("cat-file", "blob", refs[CURSOR]).decode())

    def checkpoint(self, checkpoint: Checkpoint) -> bool:
        refs = self.refs()
        head = refs.get(HEAD)
        # A fresh private index makes retries independent of any interrupted index writes.
        with tempfile.TemporaryDirectory(prefix="source-history-index-", dir=self.root.parent) as temporary:
            self.env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
            self.git("read-tree", head or "--empty")
            updates = bytearray()
            for path, digest in sorted(checkpoint.files.items()):
                if not eligible(path):
                    raise ValueError(f"ineligible history path: {path}")
                if digest is None:
                    updates.extend(f"0 {'0' * 40}\t{path}\0".encode())
                else:
                    content = self.store.path(digest).read_bytes()
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError(f"source history object hash mismatch: {path}")
                    content.decode("utf-8")
                    blob = self.git("hash-object", "-w", "--stdin", data=content).decode().strip()
                    updates.extend(f"100644 {blob}\t{path}\0".encode())
            self.git("update-index", "-z", "--index-info", data=bytes(updates))
            tree = self.git("write-tree").decode().strip()
            changed = head is None or tree != self.git("rev-parse", f"{head}^{{tree}}").decode().strip()
            commands = ["start"]
            if changed:
                timestamp = checkpoint.timestamp.replace(
                    tzinfo=checkpoint.timestamp.tzinfo or UTC
                ).isoformat()
                self.env.update(GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)
                message = (
                    f"{checkpoint.title}\n\n{checkpoint.detail}\nSource-frontier: {checkpoint.frontier}\n"
                )
                commit = (
                    self.git("commit-tree", tree, *(["-p", head] if head else []), data=message.encode())
                    .decode()
                    .strip()
                )
                commands.append(f"update {HEAD} {commit} {head or '0' * 40}")
            cursor_blob = (
                self.git("hash-object", "-w", "--stdin", data=f"{checkpoint.frontier}\n".encode())
                .decode()
                .strip()
            )
            commands.extend(
                [f"update {CURSOR} {cursor_blob} {refs.get(CURSOR, '0' * 40)}", "prepare", "commit"]
            )
            # Both refs move together: a crash cannot acknowledge an unsaved checkpoint.
            self.git("update-ref", "--stdin", data=("\n".join(commands) + "\n").encode())
            self.env.pop("GIT_INDEX_FILE", None)
            return changed

    def catch_up(self, database: Database, worktree: Path) -> dict:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        with (self.root.parent / f".{self.root.name}.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self.initialize()
            checkpoints = read_checkpoints(database, worktree, self.cursor())
            committed = sum(self.checkpoint(checkpoint) for checkpoint in checkpoints)
            # Pack periodically, including unchanged runs, to keep text history compact.
            self.git("maintenance", "run", "--auto")
            return {
                "repository": str(self.root),
                "checkpoints_created": committed,
                "source_frontier": self.cursor(),
                "head": self.refs().get(HEAD),
            }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        history = History(
            args.repository or settings.state_root / "source-history.git", ObjectStore(settings.object_root)
        )
        print(json.dumps(history.catch_up(database, settings.worktree_root)), flush=True)
    finally:
        database.close()


if __name__ == "__main__":
    main()
