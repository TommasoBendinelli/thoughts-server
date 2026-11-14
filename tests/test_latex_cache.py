# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pathlib import Path
import shutil

import pytest
from pypdf import PdfReader

from thoughts_server import latex_cache, native_renderer
from test_component_cache import declare


# The compiler output version and the hash of the compiler's sources it was recorded with.
#
# This test fails whenever one of latex_cache.COMPILER_SOURCES changes. Then decide:
#   * the change CAN alter what a compile writes (a PDF, SyncTeX, recorder or auxiliary
#     file): bump latex_cache.COMPILER_OUTPUT_VERSION and write the new version and the
#     new hash here. Every document of every Library is compiled once more;
#   * the change CANNOT alter compiled output (logging, timing, sandbox set-up, comments):
#     write only the new hash here. No cache entry is lost.
RECORDED_COMPILER_OUTPUT_VERSION = 1
RECORDED_COMPILER_SOURCES_SHA256 = "375c4cf5c90b5d58dded4e060427dfbfcc8a311eb01e1c12748cd6eca1976f51"


def test_compiler_sources_are_recorded_with_the_output_version():
    assert latex_cache.COMPILER_OUTPUT_VERSION == RECORDED_COMPILER_OUTPUT_VERSION, (
        "latex_cache.COMPILER_OUTPUT_VERSION changed: record it in RECORDED_COMPILER_OUTPUT_VERSION "
        "together with the new RECORDED_COMPILER_SOURCES_SHA256 "
        f"({latex_cache.compiler_sources_sha256()})"
    )
    assert latex_cache.compiler_sources_sha256() == RECORDED_COMPILER_SOURCES_SHA256, (
        "A compiler source file changed (" + ", ".join(latex_cache.COMPILER_SOURCES) + "). "
        "If the change can alter compiled output, bump latex_cache.COMPILER_OUTPUT_VERSION (every cached "
        "document is then compiled once more) and record the new version here. If it cannot, update only "
        f"RECORDED_COMPILER_SOURCES_SHA256 to {latex_cache.compiler_sources_sha256()}."
    )


def test_cache_name_follows_the_output_version_not_the_compilers_files(tmp_path, monkeypatch):
    """A deploy that edits the compiler keeps every entry; only a bumped version starts afresh."""
    implementation = tmp_path / "implementation"
    (implementation / "renderer_tools").mkdir(parents=True)
    for name in (*latex_cache.COMPILER_SOURCES, "native_renderer.py"):
        (implementation / name).write_text("version one")
    monkeypatch.setattr(latex_cache, "__file__", str(implementation / "latex_cache.py"))
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    project = docs / "docs-paper"
    project.mkdir(parents=True)
    before = latex_cache.cache_path(docs, project, "main.tex")
    recorded = latex_cache.compiler_sources_sha256()
    for name in ("native_renderer.py", *latex_cache.COMPILER_SOURCES):
        (implementation / name).write_text("changed without changing compiled output")
        assert latex_cache.cache_path(docs, project, "main.tex") == before
    # The guard sees the edit, so its author has to decide about the version.
    assert latex_cache.compiler_sources_sha256() != recorded
    monkeypatch.setattr(latex_cache, "COMPILER_OUTPUT_VERSION", 2)
    bumped = latex_cache.cache_path(docs, project, "main.tex")
    assert bumped != before
    monkeypatch.setattr(latex_cache, "COMPILER_OUTPUT_VERSION", 3)
    assert latex_cache.cache_path(docs, project, "main.tex") not in {before, bumped}
    # Everything else that named an entry still does.
    monkeypatch.setattr(latex_cache, "COMPILER_OUTPUT_VERSION", 1)
    assert latex_cache.cache_path(docs, project, "other.tex") != before
    (project / "latexmkrc").write_text("$pdf_mode = 1;\n")
    assert latex_cache.cache_path(docs, project, "main.tex") != before


def test_entries_written_before_the_version_constant_are_still_found(tmp_path, monkeypatch):
    """Version 1 names an entry as the release that hashed its own files did.

    The expected name was computed with that release's code for these inputs, so the
    deploy that introduces the constant finds every existing entry.
    """
    for name in ("SOURCE_DATE_EPOCH", "TEXINPUTS", "BIBINPUTS", "BSTINPUTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", "/cache")
    monkeypatch.setattr(latex_cache.shutil, "which", lambda _name: None)
    docs = tmp_path / "docs"
    (docs / "docs-paper").mkdir(parents=True)
    assert latex_cache.COMPILER_OUTPUT_VERSION == 1
    assert latex_cache.cache_path(docs, docs / "docs-paper", "main.tex") == Path(
        "/cache/f40b08f6a63867052b75e4b3338825317e9a92e38e5781152f14185fb2b09787.zip"
    )
    # A later version no longer needs the recorded hashes of that release.
    assert latex_cache.compiler_identity(2) == ["compiler-output-version", 2]


def test_restores_auxiliary_paths_but_never_a_pdf(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    build = old / "build"
    build.mkdir(parents=True)
    (build / "main.aux").write_text(f"reference {old}/source.tex")
    (build / "main.pdf").write_bytes(b"stale PDF")
    (build / "main.synctex.gz").write_bytes(b"stale map")
    archive = tmp_path / "cache.zip"
    latex_cache.save(archive, old, build, "main")
    new.mkdir()
    restored = latex_cache.restore(archive, new, new, "main")
    assert restored == [new / "main.aux"]
    assert (new / "main.aux").read_text() == f"reference {new}/source.tex"
    assert not (new / "main.pdf").exists()
    assert not (new / "main.synctex.gz").exists()
    archive.write_bytes(b"corrupt")
    assert latex_cache.restore(archive, new, new, "main") == []


@pytest.mark.skipif(not shutil.which("latexmk"), reason="requires TeX Live")
def test_warm_isolated_build_converges_to_clean_build_after_text_and_label_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    def compile_at(name, contents):
        docs = tmp_path / name
        project = docs / "docs-paper"
        source = project / "tex/source/main.tex"
        source.parent.mkdir(parents=True)
        source.write_text(contents)
        declare(docs, project, "tex/source/main.tex")
        return native_renderer._compile_source(docs, project, "main.tex")
    original = r"""\documentclass{article}
\usepackage{hyperref}
\begin{document}
\tableofcontents
\section{Original}\label{sec:first}
Original word. See Section~\ref{sec:first}.
\end{document}
"""
    compile_at("first", original)
    changed = original.replace("Original word", "Changed word").replace("sec:first", "sec:replacement")
    warm = compile_at("warm", changed)
    monkeypatch.delenv("THOUGHTS_LATEX_CACHE_ROOT")
    cold = compile_at("cold", changed)
    assert [page.extract_text() for page in PdfReader(warm.pdf).pages] == [page.extract_text() for page in PdfReader(cold.pdf).pages]
    assert "Changed word" in PdfReader(warm.pdf).pages[0].extract_text()
    assert str(tmp_path / "first") not in warm.fls.read_text()
