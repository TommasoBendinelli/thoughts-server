# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reuse compiler bookkeeping without reusing PDFs or escaping an isolated workspace."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

# Auxiliary-only reuse; exact PDF/SyncTeX reuse is validated separately by component_cache.
SUFFIXES = (".aux", ".bbl", ".blg", ".toc", ".out", ".lof", ".lot", ".bcf", ".run.xml", ".nav", ".snm", ".fdb_latexmk")

# The version of the compiler's own code, as far as it decides what a compile writes.
#
# It is part of every cache entry's name. Bump it BY HAND, in the same commit, when a
# change to the files in COMPILER_SOURCES can alter a compiled PDF, its SyncTeX, its
# recorder file or the auxiliary files: every document of every Library is then
# compiled once more. A change that cannot (logging, timing, sandbox set-up, comments)
# leaves it alone, and no cache entry is lost.
#
# Before, the name hashed those source files themselves, so any deploy that touched
# one of them threw every entry away. tests/test_latex_cache.py holds a hash of the
# files and fails when they change, to make the author decide.
COMPILER_OUTPUT_VERSION = 1
# The files whose content used to be hashed into the cache name, relative to this package.
COMPILER_SOURCES = (
    "latex_cache.py",
    "latex_compiler.py",
    "component_cache.py",
    "renderer_tools/review_instrumentation.py",
    "renderer_tools/review_source_staging.py",
    "renderer_tools/review_macro_expansion.py",
    "renderer_tools/review_macro_definitions.py",
)
# Version 1 keeps the names the server gave its entries when the constant was
# introduced: the hashes of COMPILER_SOURCES in the release that last hashed them.
# Entries written by that release are therefore found, and introducing the constant
# cost no recompile. Delete this list with the first bump.
_VERSION_1_SOURCE_HASHES = (
    "8504240f1b84b111ee4e6a5e0eeab863470eae6c382987850decbe19e71c6517",
    "a52f1da2a6cb53f9da0e4c1bcdbb882043032a734d079f2955e2e2207f003d54",
    "b388c4000199bdaeadc86787e1fab35203dc4df97919a64273e0a3ec3a8fa407",
    "2571f25fb2bd66eec03af144207778c2ab1da8dd186ef314e5813b8de185bc04",
    "55563153972710713557551266d1d2315a5f9f1ca0d01d109b5ede7b90642e9f",
    "89e97dbdd9f11525f364b4b0b57137daf6dee4bfb757c253a82614f928809c34",
    "24c5adf697c0a486f71c9325b653d032c993e47aff9b94ef7ea7caa221cb8c4c",
)


def compiler_identity(version: int | None = None) -> list:
    """What stands for the compiler's code in a cache entry's name."""
    version = COMPILER_OUTPUT_VERSION if version is None else version
    if version == 1:
        return list(_VERSION_1_SOURCE_HASHES)
    return ["compiler-output-version", version]


def compiler_sources_sha256() -> str:
    """One hash over COMPILER_SOURCES as they are now (for the test that guards the constant)."""
    digest = hashlib.sha256()
    for name in COMPILER_SOURCES:
        digest.update(name.encode() + b"\0" + (Path(__file__).parent / name).read_bytes() + b"\0")
    return digest.hexdigest()


def cache_path(docs_root: Path, project: Path, tex_name: str) -> Path | None:
    configured = os.environ.get("THOUGHTS_LATEX_CACHE_ROOT")
    if not configured:
        return None
    tools = []
    for name in ("latexmk", "pdflatex", "bibtex", "biber"):
        executable = shutil.which(name)
        stat = Path(executable).resolve().stat() if executable else None
        tools.append((name, executable, (stat.st_size, stat.st_mtime_ns) if stat else None))
    configuration = []
    for base in (docs_root, project):
        for name in (".latexmkrc", "latexmkrc"):
            path = base / name
            if path.is_file():
                configuration.append((str(path.relative_to(docs_root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    from .source_owner import folder_owner
    key = hashlib.sha256(json.dumps([
        "isolated-latex-state-v1", folder_owner(project), tex_name, tools, configuration,
        compiler_identity(),
        {name: os.environ.get(name, "") for name in ("SOURCE_DATE_EPOCH", "TEXINPUTS", "BIBINPUTS", "BSTINPUTS")},
    ], sort_keys=True).encode()).hexdigest()
    return Path(configured) / f"{key}.zip"


def restore(path: Path | None, docs_root: Path, build_dir: Path, stem: str) -> list[Path]:
    if path is None or not path.is_file():
        return []
    restored: list[Path] = []
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            old_root = metadata["docs_root"]
            entries = metadata["files"]
            for name, info in entries.items():
                if name not in {stem + suffix for suffix in SUFFIXES}:
                    raise ValueError("unexpected compiler cache member")
                raw = archive.read(name)
                if hashlib.sha256(raw).hexdigest() != info["sha256"]:
                    raise ValueError("compiler cache checksum mismatch")
                # latexmk records absolute input paths; every reference must follow the new snapshot.
                raw = raw.replace(old_root.encode(), str(docs_root).encode())
                destination = build_dir / name
                destination.write_bytes(raw)
                restored.append(destination)
                os.utime(destination, ns=(info["mtime_ns"], info["mtime_ns"]))
        return restored
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        for destination in restored:
            destination.unlink(missing_ok=True)
        return []


def clear(build_dir: Path, stem: str) -> None:
    for suffix in SUFFIXES:
        (build_dir / (stem + suffix)).unlink(missing_ok=True)


def save(path: Path | None, docs_root: Path, build_dir: Path, stem: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = {}
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for suffix in SUFFIXES:
                source = build_dir / (stem + suffix)
                if source.is_file() and not source.is_symlink():
                    raw = source.read_bytes()
                    archive.writestr(source.name, raw)
                    entries[source.name] = {"sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": source.stat().st_mtime_ns}
            archive.writestr("metadata.json", json.dumps({"docs_root": str(docs_root), "files": entries}))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
