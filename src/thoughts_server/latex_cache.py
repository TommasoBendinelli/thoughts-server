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


def cache_path(docs_root: Path, project: Path, tex_name: str) -> Path | None:
    configured = os.environ.get("THOUGHTS_LATEX_CACHE_ROOT")
    if not configured:
        return None
    tools = []
    for name in ("latexmk", "pdflatex", "bibtex", "biber"):
        executable = shutil.which(name)
        stat = Path(executable).resolve().stat() if executable else None
        tools.append((name, executable, (stat.st_size, stat.st_mtime_ns) if stat else None))
    implementations = [Path(__file__), Path(__file__).with_name("latex_compiler.py"),
                       Path(__file__).with_name("component_cache.py"),
                       Path(__file__).parent / "renderer_tools/review_instrumentation.py",
                       Path(__file__).parent / "renderer_tools/review_source_staging.py",
                       Path(__file__).parent / "renderer_tools/review_macro_expansion.py",
                       Path(__file__).parent / "renderer_tools/review_macro_definitions.py"]
    configuration = []
    for base in (docs_root, project):
        for name in (".latexmkrc", "latexmkrc"):
            path = base / name
            if path.is_file():
                configuration.append((str(path.relative_to(docs_root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    from .source_owner import folder_owner
    key = hashlib.sha256(json.dumps([
        "isolated-latex-state-v1", folder_owner(project), tex_name, tools, configuration,
        [hashlib.sha256(path.read_bytes()).hexdigest() for path in implementations],
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
