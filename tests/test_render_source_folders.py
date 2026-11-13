# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Document paths and dependency ownership, independent of directory conventions."""
import hashlib
import shutil
import uuid

import pytest
from pypdf import PdfReader

from thoughts_server import native_renderer as renderer, project_configuration as pc
from thoughts_server.project_documents import project_documents
from thoughts_server.renderer_tools import doc_links


def manifest(root, paths):
    owner = 'docs-paper'
    default = str(uuid.uuid4())
    folder = str(uuid.uuid4())
    value = {'schema_version': 5, 'kind': 'thoughts-project', 'id': 'paper', 'name': 'Paper',
             'default_folder_id': default, 'source_folders': [
                 {'folder_id': default, 'path': pc.default_folder('paper'), 'document_id': str(uuid.uuid4())},
                 {'folder_id': folder, 'path': owner, 'document_id': str(uuid.uuid4())}],
             'documents': [{'document_id': str(uuid.uuid4()), 'source': {'folder_id': folder, 'path': path},
                            'format': 'latex', 'included': True} for path in paths]}
    pc.save(root, value)
    return value


def captured(root, paths):
    return [{'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'size_bytes': path.stat().st_size} for path in paths]


def closure_fixture(tmp_path, support):
    root = tmp_path / 'docs'
    project = root / 'docs-paper'
    source = project / 'paper/main.tex'
    style = project / support
    for path, content in [(source, 'source'), (style, 'style')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    record = project / 'build/main.fls'
    record.parent.mkdir()
    record.write_text(f'INPUT {source}\nINPUT {style}\n')
    built = renderer.SourceBuild(project.name, 'paper', 'paper/main.tex', 'docs-paper/paper/main.tex', source,
                                 project / 'pdf/paper_main.pdf', None, record)
    expectation = {'target': 'paper.pdf', 'project': project.name, 'snapshot_sha256': 'a' * 64,
                   'discovery_policy': 'source-closure-v2', 'entries': [],
                   'source_entries': captured(root, [source, style])}
    return root, project, style, renderer.ProjectBuild(project.name, 'paper', (built,)), expectation


@pytest.mark.parametrize('support', ['manual/support/neurips_2026.sty',
                                    'manual/support/manual_docs_style.sty',
                                    'styles/text_facts_preprocessing_style.sty'])
def test_registered_styles_compile_independent_of_directory(tmp_path, monkeypatch, support):
    root, project, style, build, expectation = closure_fixture(tmp_path, support)
    monkeypatch.setattr(renderer, '_linked_shared_pdfs', lambda *_: [])
    result = renderer._verify_tex_closure(root, build, 'paper.pdf', expectation)
    assert style.relative_to(root).as_posix() in result['consumed_paths']


@pytest.mark.parametrize('violation', ['changed', 'uncaptured', 'other_folder', 'host_symlink', 'host_absolute', 'ignored'])
def test_dependency_verification_preserves_source_boundary(tmp_path, monkeypatch, violation):
    root, project, style, build, expectation = closure_fixture(tmp_path, 'styles/theme.sty')
    monkeypatch.setattr(renderer, '_linked_shared_pdfs', lambda *_: [])
    if violation == 'changed':
        style.write_text('changed')
    elif violation == 'uncaptured':
        expectation['source_entries'].pop()
    elif violation == 'ignored':
        (project / '.ThoughtIgnore').write_text('styles/\n')
    else:
        foreign = (root / 'docs-other' if violation == 'other_folder' else tmp_path / 'host') / 'theme.sty'
        foreign.parent.mkdir(parents=True)
        foreign.write_text('style')
        style.unlink()
        style.symlink_to(foreign)
        if violation == 'host_absolute':
            build.sources[0].fls.write_text(f'INPUT {foreign}\n')
    with pytest.raises(renderer.RenderError, match='captured input changed|undeclared_build_input|cross_project_build_input|excluded_build_input'):
        renderer._verify_tex_closure(root, build, 'paper.pdf', expectation)


@pytest.mark.parametrize('path', ['../outside.tex', '/outside.tex', 'paper/../../outside.tex'])
def test_manifest_rejects_escaping_source(tmp_path, path):
    with pytest.raises(ValueError):
        manifest(tmp_path, [path])


@pytest.mark.skipif(not shutil.which('latexmk') or not shutil.which('qpdf'), reason='requires TeX toolchain')
def test_root_and_nested_documents_compile_to_distinct_pdfs(tmp_path):
    root = tmp_path / 'docs'
    project = root / 'docs-paper'
    paths = ['main.tex', 'chapters/main.tex', 'tex/source/main.tex']
    for path, text in zip(paths, ['First document.', 'Second document.', 'Third document.']):
        source = project / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('\\documentclass{article}\n\\usepackage{styles/theme}\n'
                          '\\begin{document}\n' + text + '\n\\end{document}\n')
    style = project / 'styles/theme.sty'
    style.parent.mkdir()
    style.write_text('\\ProvidesPackage{theme}\n')
    manifest(root, paths)
    declaration_bytes = (root / pc.manifest_path('paper')).read_bytes()
    source_bytes = {p: p.read_bytes() for p in [project / p for p in paths] + [style]}
    documents = project_documents(project)
    assert len({entry.output for entry in documents}) == 3
    build = renderer._compile_project(root, project, jobs=2)
    assert [s.source_id for s in build.sources] == ['docs-paper/' + p for p in paths]
    for source, expected in zip(build.sources, ['First document.', 'Second document.', 'Third document.']):
        assert expected in ''.join(page.extract_text() for page in PdfReader(source.pdf).pages)
        target = doc_links.target_for_selector(root, source.source_id)
        assert target.pdf_path == source.pdf
    renderer._verify_source_project_inputs(root, build)
    renderer._verify_tex_closure(root, build, 'paper.pdf', {
        'target': 'paper.pdf', 'project': project.name, 'snapshot_sha256': 'a'*64,
        'discovery_policy': 'source-closure-v2', 'entries': [],
        'source_entries': captured(root, list(source_bytes))})
    publication = root / 'current_pdfs'
    publication.mkdir()
    # d0e79a6: each document publishes its own PDF; Projects are no longer combined.
    published = [renderer._publish_document_pdf(root, renderer.ProjectBuild(build.project, build.project_short, (source,)),
                                                publication, render_validation=False)
                 for source in build.sources]
    assert len({path.name for path in published}) == 3
    assert {path.name for path in published} == {entry.output for entry in documents}
    for path, expected in zip(published, ['First document.', 'Second document.', 'Third document.']):
        pages = PdfReader(path).pages
        assert len(pages) == 1
        assert expected in pages[0].extract_text()
    assert (root / pc.manifest_path('paper')).read_bytes() == declaration_bytes
    assert all(p.read_bytes() == data for p, data in source_bytes.items())


@pytest.mark.parametrize('changed', [False, True])
def test_sparse_worker_copies_only_captured_sources(settings, tmp_path, changed):
    from dataclasses import replace
    from thoughts_server.builds import BuildWorker
    from thoughts_server.database import Database
    from thoughts_server.models import BuildJob
    from thoughts_server.object_store import ObjectStore
    root, project, style, build, expectation = closure_fixture(tmp_path, 'styles/theme.sty')
    manifest(root, ['paper/main.tex'])
    from test_cross_tex_links import selected_source
    external = selected_source(root, 'docs-concepts', 'tex/source/attention.tex')
    stray = project / 'styles/unregistered.sty'
    stray.write_text('unregistered')
    if changed:
        style.write_text('modified after capture')
    database = Database(settings.database_url)
    database.create_schema()
    worker = BuildWorker(replace(settings, docs_root=root, worktree_root=root),
                         database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(id='custom-folder-workspace', target='paper.pdf', progress={})
    try:
        if changed:
            with pytest.raises(RuntimeError, match='captured source changed'):
                worker._prepare_build_workspace(job, [expectation])
        else:
            _, workspace, _, _ = worker._prepare_build_workspace(job, [expectation])
            assert (workspace / 'docs-paper/styles/theme.sty').read_bytes() == style.read_bytes()
            assert (workspace / 'docs-paper/paper/main.tex').is_file()
            assert not (workspace / 'docs-paper/styles/unregistered.sty').exists()
            assert project_documents(workspace / project.name)[0].source == 'paper/main.tex'
            assert not (workspace / external.relative_to(root)).exists()
            target = doc_links.target_for_selector(workspace, 'docs-concepts/attention.tex')
            assert target.source_path == workspace / external.relative_to(root)
            assert not (workspace / 'docs-concepts/thoughts-project.json').exists()
    finally:
        database.close()
