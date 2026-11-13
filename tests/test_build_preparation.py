# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
import hashlib
import json
import shutil

import pytest
from pypdf import PdfReader, PdfWriter

from thoughts_server import latex_compiler, native_renderer
from thoughts_server.renderer_tools.shipout_placements import shipout_markers
from test_component_cache import declare


def make_project(docs, *, changed=False, name="docs-paper"):
    project = docs / name
    sources = project / "tex/source"
    sources.mkdir(parents=True)
    names = [f"part{n}.tex" for n in range(7)]
    (project / "tex/docs_order_manifest.txt").write_text("\n".join(names) + "\n")
    assets = project / "tex/generated/figures"
    assets.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=30)
    writer.write(assets / "scatter.pdf")
    for n in range(80):
        (assets / f"asset{n}.dat").write_bytes(b"shared immutable asset")
    (project / "tex/shared.tex").write_text("Shared included text.\n")
    for n, name in enumerate(names):
        word = "Changed" if changed and n == 0 else "Original"
        (sources / name).write_text(
            r"\documentclass{article}" "\n"
            r"\usepackage{graphicx,hyperref}" "\n"
            r"\begin{document}" "\n"
            + f"Part {n}. {word} word.\n"
            + rf"\CommentHighlight{{highlight-{n}}}{{Note}}{{Selected words.}}" "\n"
            + r"\input{tex/shared.tex}" "\n"
            + r"\includegraphics{tex/generated/figures/scatter.pdf}" "\n"
            + r"\end{document}" "\n"
        )
    return project, names


def test_concurrent_preparation_has_one_writer_and_complete_shared_assets(tmp_path, monkeypatch):
    project, names = make_project(tmp_path)
    original = latex_compiler.stage_review_sources
    calls = []
    entered, release = Event(), Event()

    def prepare(*args):
        calls.append(args)
        entered.set()
        assert release.wait(10)
        return original(*args)

    monkeypatch.setattr(latex_compiler, "stage_review_sources", prepare)
    preparation = latex_compiler.BuildPreparation()
    barrier = Barrier(5)

    def worker(n):
        if n < 5:
            barrier.wait(10)
        return preparation.prepare(project, project / "tex/source" / names[n])

    with ThreadPoolExecutor(max_workers=5) as pool:
        pending = [pool.submit(worker, n) for n in range(7)]
        assert entered.wait(10)
        assert not any(future.done() for future in pending)
        release.set()
        results = [future.result(timeout=20) for future in pending]

    assert len(calls) == 1
    assert len({staged.project for _, staged in results}) == 1
    for (_, staged), name in zip(results, names):
        assert staged.source.name == name
        assert staged.source.is_file()
        assert (staged.project / "tex/generated/figures/scatter.pdf").read_bytes() == (
            project / "tex/generated/figures/scatter.pdf"
        ).read_bytes()
        assert len(json.loads(staged.manifest.read_text())["files"]) == 8


def test_failed_preparation_is_shared_and_new_build_can_retry(tmp_path, monkeypatch):
    project, names = make_project(tmp_path)
    original = latex_compiler.stage_review_sources
    failure = OSError("staging failed after writing a partial tree")
    calls = []

    def fail(*args):
        calls.append(args)
        raise failure

    monkeypatch.setattr(latex_compiler, "stage_review_sources", fail)
    preparation = latex_compiler.BuildPreparation()
    with ThreadPoolExecutor(max_workers=5) as pool:
        pending = [pool.submit(preparation.prepare, project, project / "tex/source" / n) for n in names]
        for future in pending:
            assert future.exception(timeout=10) is failure
    assert len(calls) == 1
    monkeypatch.setattr(latex_compiler, "stage_review_sources", original)
    _, staged = latex_compiler.BuildPreparation().prepare(project, project / "tex/source" / names[0])
    assert staged.source.is_file()


def test_different_projects_can_prepare_concurrently(tmp_path, monkeypatch):
    projects = [make_project(tmp_path, name=f"docs-project{n}")[0] for n in range(2)]
    original = latex_compiler.stage_review_sources
    barrier = Barrier(2)

    def prepare(*args):
        barrier.wait(10)
        return original(*args)

    monkeypatch.setattr(latex_compiler, "stage_review_sources", prepare)
    preparation = latex_compiler.BuildPreparation()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(preparation.prepare, p, p / "tex/source/part0.tex") for p in projects]
        assert len({future.result(timeout=20)[1].project for future in pending}) == 2


@pytest.mark.skipif(not shutil.which("latexmk"), reason="requires TeX Live")
def test_real_parallel_compile_preserves_assets_markers_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    original = latex_compiler.stage_review_sources
    calls = []
    lock = Lock()

    def prepare(*args):
        with lock:
            calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(latex_compiler, "stage_review_sources", prepare)

    def build(label, *, changed=False):
        docs = tmp_path / label
        project, names = make_project(docs, changed=changed)
        declare(docs, project, *(f"tex/source/{name}" for name in names))
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in project.rglob("*") if p.is_file()}
        result = native_renderer._compile_project(docs, project, jobs=5)
        assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items())
        assert calls.count(project.resolve()) == 1
        assert [s.tex_name for s in result.sources] == names
        return result.sources

    cold = build("cold")
    assert not any(s.compile_cache_hit for s in cold)
    warm = build("warm")
    assert all(s.compile_cache_hit for s in warm)
    edited = build("edited", changed=True)
    assert [s.compile_cache_hit for s in edited] == [False] + [True] * 6
    monkeypatch.delenv("THOUGHTS_LATEX_CACHE_ROOT")
    fresh = build("fresh", changed=True)

    for n, (cached, rebuilt) in enumerate(zip(edited, fresh)):
        a, b = PdfReader(cached.pdf), PdfReader(rebuilt.pdf)
        assert [p.extract_text() for p in a.pages] == [p.extract_text() for p in b.pages]
        assert shipout_markers(a) == shipout_markers(b)
        assert f"comment:highlight-{n}" in shipout_markers(a)
        assert len(a.pages[0].get("/Resources").get("/XObject")) == 1
        assert str(tmp_path / "cold") not in cached.fls.read_text()
