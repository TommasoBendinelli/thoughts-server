# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pathlib import Path
import shutil

import pytest
from pypdf import PdfReader

from thoughts_server import latex_cache, native_renderer
from test_component_cache import declare


def test_publication_changes_preserve_cache_but_compiler_changes_invalidate(tmp_path, monkeypatch):
    implementation = tmp_path / "implementation"
    (implementation / "renderer_tools").mkdir(parents=True)
    for name in ("latex_cache.py", "latex_compiler.py", "native_renderer.py", "component_cache.py",
                 "renderer_tools/review_instrumentation.py", "renderer_tools/review_source_staging.py",
                 "renderer_tools/review_macro_expansion.py", "renderer_tools/review_macro_definitions.py"):
        (implementation / name).write_text("version one")
    monkeypatch.setattr(latex_cache, "__file__", str(implementation / "latex_cache.py"))
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    docs = tmp_path / "docs"
    project = docs / "docs-paper"
    project.mkdir(parents=True)
    before = latex_cache.cache_path(docs, project, "main.tex")
    (implementation / "native_renderer.py").write_text("changed publishing and progress reporting")
    assert latex_cache.cache_path(docs, project, "main.tex") == before
    (implementation / "latex_compiler.py").write_text("changed compilation behavior")
    assert latex_cache.cache_path(docs, project, "main.tex") != before


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
