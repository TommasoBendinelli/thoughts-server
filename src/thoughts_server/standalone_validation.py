# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Cached, isolated validation of an immutable standalone TeX repository on Fedora."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pypdf import PdfReader

MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_INPUT_FILES = 4096


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def relative_file(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts
            or "\\" in value or str(path) != value or value.startswith("-")):
        raise ValueError("standalone input must be a normalized repository-relative file")
    return value


class StandaloneValidationRequest(BaseModel):
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: str = Field(max_length=512)

    @field_validator("source")
    @classmethod
    def safe_source(cls, value: str) -> str:
        relative_file(value)
        if not value.endswith(".tex"):
            raise ValueError("standalone source must be a TeX file")
        return value


class StandaloneValidationRecord(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    executor: Literal["fedora"] = "fedora"
    bundle_sha256: str
    source: str
    toolchain_sha256: str
    cache_hit: bool = False
    pdf_sha256: str | None = None
    log_sha256: str | None = None
    pages: int | None = None
    seconds: float | None = None
    error: str | None = None


def unpack_bundle(archive: Path, destination: Path) -> None:
    """Only regular Git files, with bounded size, no links or path traversal."""
    size = 0
    names = set()
    with tarfile.open(archive, "r:") as bundle:
        for entry in bundle:
            name = relative_file(entry.name)
            if not entry.isfile() or name in names:
                raise ValueError("standalone bundle contains a link, duplicate or non-file entry")
            names.add(name)
            size += entry.size
            if size > MAX_INPUT_BYTES or len(names) > MAX_INPUT_FILES:
                raise ValueError("standalone bundle exceeds the input limit")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)


def toolchain_identity(configured_key: str) -> str:
    files = [Path(__file__)]
    for name in ("bwrap", "latexmk", "pdflatex", "bibtex", "biber", "qpdf"):
        executable = shutil.which(name)
        if executable:
            files.append(Path(executable).resolve())
    return digest(json.dumps([configured_key, [(str(p), digest(p.read_bytes())) for p in files]],
                             sort_keys=True).encode())


def sandbox_command(workspace: Path, source: str) -> list[str]:
    executable = shutil.which("bwrap")
    if executable is None:
        raise RuntimeError("Standalone validation on the server requires bubblewrap")
    command = [executable, "--die-with-parent", "--unshare-all", "--new-session",
               "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64"):
        if Path("/" + name).exists():
            command += ["--symlink", "usr/" + name, "/" + name]
    # Expose TeX/font configuration, never the server worktree, credentials or home.
    for path in ("/etc/texlive", "/etc/texmf", "/etc/fonts", "/etc/ld.so.cache", "/var/lib/texmf",
                 "/var/lib/texlive",
                 "/var/cache/fontconfig"):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--dir", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--dir", "/home/tex", "--ro-bind", str(workspace / "inputs"), "/project",
                "--bind", str(workspace / "output"), "/output", "--chdir", "/project",
                "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
                "--setenv", "HOME", "/home/tex", "--setenv", "LANG", "C.UTF-8",
                "--setenv", "SOURCE_DATE_EPOCH", "946684800",
                "--setenv", "openin_any", "p", "--setenv", "openout_any", "p",
                "--setenv", "shell_escape", "f",
                "latexmk", "-norc", "-pdf", "-synctex=1", "-interaction=nonstopmode",
                "-halt-on-error", "-file-line-error", "-latexoption=-no-shell-escape",
                "-output-directory=/output", source]
    return command


def compile_standalone(workspace: Path, source: str, timeout: int) -> dict:
    log = workspace / "compiler.log"
    with log.open("wb") as output:
        process = subprocess.Popen(sandbox_command(workspace, source), stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise RuntimeError(f"Standalone compilation on the server exceeded {timeout} seconds")
    if code:
        raise RuntimeError("Standalone compilation on the server failed: " +
                           " ".join(log.read_text(errors="replace")[-3000:].split()))
    stem = Path(source).stem
    pdf = workspace / "output" / (stem + ".pdf")
    subprocess.run(["qpdf", "--check", str(pdf)], check=True, capture_output=True, timeout=30)
    dependencies = {}
    for line in (workspace / "output" / (stem + ".fls")).read_text().splitlines():
        if not line.startswith("INPUT "):
            continue
        path = Path(line[6:])
        # Project/output files are frozen or generated; other reads are sandboxed system inputs.
        if path.is_absolute() and not path.is_relative_to("/project") and not path.is_relative_to("/output"):
            if path.is_file():
                dependencies[str(path)] = digest(path.read_bytes())
    return {"pdf": pdf, "pages": len(PdfReader(pdf).pages), "dependencies": dependencies}


class StandaloneValidations:
    def __init__(self, settings, object_store, session_factory):
        self.settings = settings
        self.store = object_store
        self.session_factory = session_factory
        self.root = settings.state_root / "standalone-validations"
        self.root.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="standalone-validation")
        self.lock = threading.Lock()
        self.futures = {}

    def close(self):
        self.pool.shutdown(wait=True)

    def _write(self, key, value):
        descriptor, name = tempfile.mkstemp(dir=self.root, prefix=".status-")
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(value, stream, sort_keys=True)
            os.replace(name, self.root / (key + ".json"))
        finally:
            Path(name).unlink(missing_ok=True)

    def _read(self, key):
        try:
            return json.loads((self.root / (key + ".json")).read_text())
        except FileNotFoundError:
            return None

    def _valid(self, record):
        if not record or record["status"] != "succeeded":
            return False
        try:
            return (self.store.matches(record["pdf_sha256"], record["pdf_size_bytes"])
                    and all(digest(Path(p).read_bytes()) == sha
                            for p, sha in record["dependencies"].items()))
        except OSError:
            return False

    def _schedule(self, key, record):
        with self.lock:
            future = self.futures.get(key)
            if future is None or future.done():
                self.futures[key] = self.pool.submit(self._run, key, record)
            # Finished jobs need no in-memory retention; durable receipts provide the cache.
            for old in list(self.futures):
                if old != key and self.futures[old].done():
                    del self.futures[old]

    def submit(self, request: StandaloneValidationRequest):
        archive = self.store.path(request.bundle_sha256)
        if not archive.is_file():
            raise FileNotFoundError("standalone input bundle is not uploaded")
        if archive.stat().st_size > MAX_INPUT_BYTES + MAX_INPUT_FILES * 2048:
            raise ValueError("standalone input bundle is too large")
        toolchain = toolchain_identity(self.settings.toolchain_key)
        key = digest(json.dumps([request.model_dump(), toolchain], sort_keys=True).encode())
        record = {"id": key, "status": "queued", "executor": "fedora",
                  **request.model_dump(), "toolchain_sha256": toolchain, "cache_hit": False}
        with (self.root / (key + ".lock")).open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._read(key) or record
            old = self._read(key)
            if self._valid(old):
                return dict(old, cache_hit=True)
            # Replace a failed or stale terminal receipt before returning the new request.
            self._write(key, record)
            self._schedule(key, record)
        return record

    def running(self, key: str):
        """The future of a validation compiling in this process, or None."""
        with self.lock:
            future = self.futures.get(key)
        return None if future is None or future.done() else future

    def get(self, key: str, wait_seconds: int = 0):
        self.store.validate_sha256(key)
        deadline = time.monotonic() + wait_seconds
        while True:
            record = self._read(key)
            with self.lock:
                future = self.futures.get(key)
            if record and record["status"] in {"queued", "running"} and (future is None or future.done()):
                # A request interrupted by an API restart resumes from the immutable bundle.
                self._schedule(key, record)
            if record and record["status"] in {"succeeded", "failed"}:
                return record
            if time.monotonic() >= deadline:
                return record
            time.sleep(0.1)

    def _run(self, key, record):
        with (self.root / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._valid(self._read(key)):
                return
            self._write(key, dict(record, status="queued"))
            # One standalone compiler across API processes; normal PDF workers stay independent.
            with (self.root / "compiler.lock").open("a") as compiler_lock:
                fcntl.flock(compiler_lock, fcntl.LOCK_EX)
                started = time.monotonic()
                record = dict(record, status="running", cache_hit=False)
                self._write(key, record)
                with tempfile.TemporaryDirectory(prefix="compile-", dir=self.root) as directory:
                    workspace = Path(directory)
                    (workspace / "inputs").mkdir()
                    (workspace / "output").mkdir()
                    try:
                        archive = self.store.path(record["bundle_sha256"])
                        if digest(archive.read_bytes()) != record["bundle_sha256"]:
                            raise ValueError("standalone input bundle failed identity verification")
                        unpack_bundle(archive, workspace / "inputs")
                        if not (workspace / "inputs" / record["source"]).is_file():
                            raise ValueError("standalone main source is missing from the Git snapshot")
                        result = compile_standalone(workspace, record["source"],
                                                    min(300, self.settings.build_timeout_seconds))
                        with self.session_factory() as session:
                            pdf = self.store.ingest_file(session, result["pdf"], "application/pdf")
                            record.update(status="succeeded", pages=result["pages"],
                                          pdf_sha256=pdf.sha256, pdf_size_bytes=pdf.size_bytes,
                                          dependencies=result["dependencies"], error=None)
                            session.commit()
                    except Exception as error:
                        record.update(status="failed", error=str(error)[:4000])
                    finally:
                        log = workspace / "compiler.log"
                        if log.is_file():
                            with self.session_factory() as session:
                                record["log_sha256"] = self.store.ingest_file(session, log, "text/plain").sha256
                                session.commit()
                        record["seconds"] = round(time.monotonic() - started, 3)
                        self._write(key, record)
