# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A build of some notes of a Project does the work of those notes, and publishes the same bytes.

Before, such a build compile-checked every document of the Project and resolved the
links of every one of them. Now it compiles the documents it must produce and the
documents those link to. Each case here is built twice from the same state, once with
the old whole-Project work (THOUGHTS_RENDER_WHOLE_PROJECT=1) and once without, in a
fresh workspace at the same path as a real build has, and every published file is
compared byte for byte.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from test_native_renderer import configure_current_fixture
from thoughts_server import native_renderer
from thoughts_server.project_documents import project_documents

pytestmark = pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="requires latexmk and qpdf",
)

PREAMBLE = "\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n"
NOTES = {
    # alpha links to beta and to another Project; gamma links to alpha; two notes share a file.
    "alpha": PREAMBLE + "\\section{Alpha}\\SourceLabel{sec:alpha}\nAlpha text.\n\\input{tex/shared/common.tex}\n"
             "See \\SourceLink{tex/source/beta.tex}{sec:beta}{the beta note} and "
             "\\CrossSourceLink{docs-other/main.tex}{sec:other}{the other Project}.\n\\end{document}\n",
    "beta": PREAMBLE + "\\section{Beta}\\SourceLabel{sec:beta}\nBeta text.\n\\input{tex/shared/common.tex}\n"
            "\\end{document}\n",
    "gamma": PREAMBLE + "\\section{Gamma}\nGamma text, see \\SourceLink{tex/source/alpha.tex}{sec:alpha}{alpha}"
             " and \\SourceLink{tex/source/alpha.tex}{sec:missing}{a label alpha never had}.\n\\end{document}\n",
    "delta": PREAMBLE + "\\section{Delta}\nDelta text.\n\\end{document}\n",
    "epsilon": PREAMBLE + "\\section{Epsilon}\nEpsilon text.\n\\end{document}\n",
}
OTHER = PREAMBLE + "\\section{Other}\\SourceLabel{sec:other}\nOther text.\n\\end{document}\n"
COMMON = "Shared sentence, first wording.\n"


def tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


class Library:
    """One Library's durable state between builds: published PDFs, comment layouts, compiler cache."""

    def __init__(self, root: Path, whole_project: bool):
        self.root = root
        self.whole_project = whole_project
        self.notes = dict(NOTES)
        self.common = COMMON

    def build(self, workspace: Path, targets: list[str] | None, monkeypatch, capsys) -> dict:
        """Build ``targets`` (note names, None for everything) in a fresh workspace; return what it published."""
        artifacts = workspace.parent / "source-bundle"
        for stale in (workspace, artifacts):
            shutil.rmtree(stale, ignore_errors=True)
        notes = workspace / "docs-notes"
        (notes / "tex/source").mkdir(parents=True)
        (notes / "tex/shared").mkdir()
        for name, text in self.notes.items():
            (notes / f"tex/source/{name}.tex").write_text(text)
        (notes / "tex/shared/common.tex").write_text(self.common)
        (notes / "tex/docs_order_manifest.txt").write_text("".join(f"{name}.tex\n" for name in self.notes))
        other = workspace / "docs-other"
        (other / "tex/source").mkdir(parents=True)
        (other / "tex/source/main.tex").write_text(OTHER)
        (other / "tex/docs_order_manifest.txt").write_text("main.tex\n")
        (workspace / "library").mkdir()
        (workspace / "library/source-project-policy.json").write_text(
            json.dumps({"schema_version": 1, "excluded_projects": []}) + "\n")
        # What the worker restores into a workspace: the published PDFs and the comment state.
        for name in ("current_pdfs", "comments"):
            if (self.root / name).is_dir():
                shutil.copytree(self.root / name, workspace / name)
            else:
                (workspace / name).mkdir()
        index = workspace / "current_pdfs/.thoughts-source-index.json"
        if not index.is_file():
            index.write_text(json.dumps({"schema_version": 1, "pdfs": []}) + "\n")
        configure_current_fixture(workspace)

        monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(self.root / "latex-cache"))
        if self.whole_project:
            monkeypatch.setenv(native_renderer.WHOLE_PROJECT_ENVIRONMENT, "1")
        else:
            monkeypatch.delenv(native_renderer.WHOLE_PROJECT_ENVIRONMENT, raising=False)
        checked: list[str] = []
        linked: list[str] = []
        compile_source = native_renderer._compile_source
        resolve = native_renderer._resolve_source_links_and_anchors

        def counted_compile(docs_root, project, name, **options):
            checked.append(f"{project.name}/{name}")
            return compile_source(docs_root, project, name, **options)

        def counted_resolve(docs_root, source, *arguments, **options):
            linked.append(source.source_id)
            return resolve(docs_root, source, *arguments, **options)

        monkeypatch.setattr(native_renderer, "_compile_source", counted_compile)
        monkeypatch.setattr(native_renderer, "_resolve_source_links_and_anchors", counted_resolve)
        outputs_of = {Path(document.source).stem: document.output
                      for document in project_documents(workspace / "docs-notes")}
        capsys.readouterr()
        published = []
        requests = (
            [("docs-other", None), ("docs-notes", None)] if targets is None
            else [("docs-notes", [outputs_of[name] for name in targets])]
        )
        try:
            for project, names in requests:
                names = names or [document.output for document in project_documents(workspace / project)]
                published += native_renderer.render(
                    workspace, workspace / "current_pdfs", artifacts, targets=names, jobs=2,
                    render_validation=False, prepare_comments=False,
                )
        finally:
            monkeypatch.setattr(native_renderer, "_compile_source", compile_source)
            monkeypatch.setattr(native_renderer, "_resolve_source_links_and_anchors", resolve)
        log = capsys.readouterr().out
        result = {
            "outputs": sorted(path.name for path in published),
            "publications": tree(workspace / "current_pdfs"),
            "artifacts": tree(artifacts),
            "comments": tree(workspace / "comments"),
            # What the worker reads from the renderer about the documents it publishes.
            "reported": sorted(line for line in log.splitlines() if line.startswith("THOUGHTS_ARTIFACT\t")),
            "warnings": sorted(line for line in log.splitlines() if line.startswith("MJPDF_WARNING\t")),
            "checked": sorted(checked),
            "linked": sorted(linked),
            "names": outputs_of,
        }
        for name in ("current_pdfs", "comments"):
            shutil.rmtree(self.root / name, ignore_errors=True)
            shutil.copytree(workspace / name, self.root / name)
        return result


PUBLISHED = ("outputs", "publications", "artifacts", "comments", "reported")


def notes(*names: str) -> list[str]:
    return sorted(f"docs-notes/{name}.tex" for name in names)


def sources(*names: str) -> list[str]:
    return sorted(f"docs-notes/tex/source/{name}.tex" for name in names)


def test_a_build_of_some_notes_publishes_the_same_bytes_with_less_work(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "build-workspaces/job/workspace"
    before = Library(tmp_path / "before", whole_project=True)
    after = Library(tmp_path / "after", whole_project=False)

    def build_both(targets, change=None):
        results = []
        for library in (before, after):
            if change is not None:
                change(library)
            results.append(library.build(workspace, targets, monkeypatch, capsys))
        old, new = results
        for part in PUBLISHED:
            assert new[part] == old[part], part
        # Warnings of the documents the build publishes are the same ones.
        produced = set(new["outputs"])
        assert [line for line in new["warnings"] if line.split("\t")[1] in produced] == [
            line for line in old["warnings"] if line.split("\t")[1] in produced]
        return old, new

    # Everything once: both ways do the same work.
    old, new = build_both(None)
    assert len(new["outputs"]) == 6 and new["checked"] == old["checked"]
    names = new["names"]
    assert set(new["publications"]) == {".thoughts-source-index.json", "other_main.pdf", *names.values()}

    # 1. One note changes. It links to beta, so beta's component is needed; nothing else is.
    def edit_alpha(library):
        library.notes["alpha"] = library.notes["alpha"].replace("Alpha text.", "Alpha text, edited.")

    old, new = build_both(["alpha"], edit_alpha)
    assert new["outputs"] == [names["alpha"]]
    assert old["checked"] == notes("alpha", "beta", "gamma", "delta", "epsilon")
    assert old["linked"] == sources("alpha", "beta", "gamma", "delta", "epsilon")
    assert new["checked"] == notes("alpha", "beta") and new["linked"] == sources("alpha")
    # The link to beta and the link to the other Project are live in the published PDF.
    assert not [line for line in new["warnings"] if line.split("\t")[1] == names["alpha"]]
    # Before, the build also reported gamma's dead link, in a PDF it did not publish.
    assert [line.split("\t")[1] for line in old["warnings"]] == [names["gamma"]] and new["warnings"] == []

    # 2. A file two notes share changes: the build is asked for both.
    def edit_common(library):
        library.common = "Shared sentence, second wording.\n"

    old, new = build_both(["alpha", "beta"], edit_common)
    assert new["outputs"] == sorted([names["alpha"], names["beta"]])
    assert new["checked"] == notes("alpha", "beta") and new["linked"] == sources("alpha", "beta")
    assert len(old["checked"]) == 5

    # 3. A new note that links to an existing one.
    def add_zeta(library):
        library.notes["zeta"] = (
            PREAMBLE + "\\section{Zeta}\nZeta text, see \\SourceLink{tex/source/alpha.tex}{sec:alpha}{alpha}."
            "\n\\end{document}\n")

    old, new = build_both(["zeta"], add_zeta)
    zeta = new["names"]["zeta"]
    assert new["outputs"] == [zeta] and zeta in new["publications"]
    # alpha is needed for its label; what alpha itself links to is not.
    assert new["checked"] == notes("alpha", "zeta") and new["linked"] == sources("zeta")
    assert len(old["checked"]) == 6
    assert new["warnings"] == []

    # 4. A note is removed; a note that linked to it is built. Its link stays inactive, as before.
    def remove_beta(library):
        del library.notes["beta"]
        library.notes["alpha"] = library.notes["alpha"].replace("edited.", "edited again.")

    old, new = build_both(["alpha"], remove_beta)
    assert new["outputs"] == [names["alpha"]]
    assert new["checked"] == notes("alpha") and new["linked"] == sources("alpha")
    assert len(old["checked"]) == 5
    [warning] = [line for line in new["warnings"] if line.split("\t")[1] == names["alpha"]]
    assert "beta" in warning

    # 5. A note with a dead link is built: the warning is reported by the build that publishes it.
    def edit_gamma(library):
        library.notes["gamma"] = library.notes["gamma"].replace("Gamma text", "Gamma text, edited")

    old, new = build_both(["gamma"], edit_gamma)
    assert new["checked"] == notes("alpha", "gamma")
    assert [line.split("\t")[1] for line in new["warnings"]] == [names["gamma"]]

    # The work no longer grows with the notes the build does not touch.
    def add_unrelated(library):
        for index in range(4):
            library.notes[f"extra{index}"] = PREAMBLE + f"\\section{{Extra {index}}}\nExtra.\n\\end{{document}}\n"
        library.notes["delta"] = library.notes["delta"].replace("Delta text.", "Delta text, edited.")

    old, new = build_both(["delta"], add_unrelated)
    assert new["checked"] == notes("delta") and new["linked"] == sources("delta")
    assert len(old["checked"]) == 9 and len(old["linked"]) == 9


def test_every_document_of_a_project_requested_keeps_the_single_pass(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    project = docs / "docs-notes"
    (project / "tex/source").mkdir(parents=True)
    for name in ("alpha", "beta"):
        (project / f"tex/source/{name}.tex").write_text(PREAMBLE + "Text.\n\\end{document}\n")
    (project / "tex/docs_order_manifest.txt").write_text("alpha.tex\nbeta.tex\n")
    configure_current_fixture(docs)
    alpha, beta = (document.output for document in project_documents(project))
    monkeypatch.delenv(native_renderer.WHOLE_PROJECT_ENVIRONMENT, raising=False)
    assert native_renderer._requested_manifest_sources(project, {alpha}) == {"alpha.tex"}
    assert native_renderer._requested_manifest_sources(project, {alpha, beta}) is None
    assert native_renderer._requested_manifest_sources(project, set()) is None
    monkeypatch.setenv(native_renderer.WHOLE_PROJECT_ENVIRONMENT, "1")
    assert native_renderer._requested_manifest_sources(project, {alpha}) is None


def test_the_whole_project_switch_reaches_the_renderer_process(settings, monkeypatch):
    from thoughts_server.builds import BuildWorker
    from thoughts_server.database import Database
    from thoughts_server.models import BuildJob
    from thoughts_server.object_store import ObjectStore

    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(id="job", target="all")
    monkeypatch.delenv(native_renderer.WHOLE_PROJECT_ENVIRONMENT, raising=False)
    assert native_renderer.WHOLE_PROJECT_ENVIRONMENT not in worker._renderer_environment(job)
    monkeypatch.setenv(native_renderer.WHOLE_PROJECT_ENVIRONMENT, "1")
    assert worker._renderer_environment(job)[native_renderer.WHOLE_PROJECT_ENVIRONMENT] == "1"
    database.close()
