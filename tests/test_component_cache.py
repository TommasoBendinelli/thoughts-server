# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import gzip
import hashlib
import json
import shutil
import uuid
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader
from thoughts_server import component_cache, native_renderer, project_configuration as pc
from thoughts_server.renderer_tools import synctex_canonical


def fixture(docs):
    project = docs / 'docs-paper'
    build = project / 'build'
    (project / 'tex/source').mkdir(parents=True)
    (build / 'render_wrappers').mkdir(parents=True)
    for name in ('main', 'other'):
        (project / f'tex/source/{name}.tex').write_text('Original text')
    (project / 'shared.sty').write_text('shared style')
    (project / 'refs.bib').write_text('bibliography')
    (build / 'render_wrappers/main.tex').write_text(str(docs) + '/docs-paper/tex/source/main.tex')
    (build / 'main.fls').write_text(''.join(f'INPUT {project / name}\n' for name in ['tex/source/main.tex','shared.sty','build/render_wrappers/main.tex']))
    (build / 'main.fdb_latexmk').write_text(f'  "{project}/refs.bib" 123 456 "hash" ""\n')
    (build / 'main.aux').write_text('aux')
    (build / 'main.pdf').write_bytes(b'validated PDF')
    (build / 'main.synctex.gz').write_bytes(gzip.compress(f'SyncTeX Version:1\nInput:1:{project}/tex/source/main.tex\n!12\n'.encode()))
    return project


def declare(docs, project, *sources, outputs=None):
    """Select LaTeX sources in the current Project manifest, the sole runtime authority (a96d567).

    Stable identities keep separately rooted builds of the same Project comparable.
    outputs pins a source's PDF name, as a renamed folder keeps its earlier names."""
    def stable(name):
        return str(uuid.UUID(bytes=hashlib.sha256(f'{project.name}/{name}'.encode()).digest()[:16], version=4))
    value = {'schema_version': 5, 'kind': 'thoughts-project', 'id': project.name, 'name': project.name,
             'default_folder_id': stable('default'), 'source_folders': [
                 {'folder_id': stable('default'), 'path': pc.default_folder(project.name),
                  'document_id': stable('default-document')},
                 {'folder_id': stable('folder'), 'path': project.name, 'document_id': stable('folder-document')}],
             'documents': [{'document_id': stable(source), 'source': {'folder_id': stable('folder'), 'path': source},
                            'format': 'latex', 'included': True} for source in sources]}
    if outputs:
        value['rendering'] = {stable(source): {'output': output} for source, output in outputs.items()}
    pc.save(docs, value)


def test_reuse_rebases_paths_and_ignores_other_source_edits(tmp_path):
    old, new = tmp_path/'old', tmp_path/'new-longer-root'
    first = fixture(old)
    second = fixture(new)
    cache = tmp_path/'cache.zip'
    component_cache.save(cache, old, first, 'main')
    (second/'tex/source/other.tex').write_text('An unrelated edit')
    assert component_cache.restore(cache, new, second, 'main')
    assert str(old) not in (second/'build/main.fls').read_text()
    a = (first/'build/main.synctex.gz').read_bytes()
    b = (second/'build/main.synctex.gz').read_bytes()
    assert str(old).encode() not in gzip.decompress(b)
    assert synctex_canonical.canonical_payload(a, old) == synctex_canonical.canonical_payload(b, new)


@pytest.mark.parametrize('changed', ['tex/source/main.tex', 'shared.sty', 'refs.bib', 'optional.tex', 'build/render_wrappers/main.tex'])
def test_dependency_or_inventory_changes_invalidate(tmp_path, changed):
    docs=tmp_path/'docs'; project=fixture(docs); cache=tmp_path/'cache.zip'
    component_cache.save(cache, docs, project, 'main')
    (project/changed).write_text('changed')
    assert not component_cache.restore(cache, docs, project, 'main')


def test_corrupt_archive_never_installs_partial_output(tmp_path):
    docs=tmp_path/'docs'; project=fixture(docs); cache=tmp_path/'cache.zip'
    component_cache.save(cache, docs, project, 'main')
    with zipfile.ZipFile(component_cache.archive_path(cache), 'a') as archive:
        metadata=json.loads(archive.read('metadata.json'))
        metadata['outputs']['main.pdf']='0'*64
        archive.writestr('metadata.json', json.dumps(metadata))
    (project/'build/main.aux').write_text('untouched')
    assert not component_cache.restore(cache, docs, project, 'main')
    assert (project/'build/main.aux').read_text()=='untouched'


@pytest.mark.skipif(not shutil.which('latexmk'), reason='requires TeX Live')
def test_actual_compiler_reuses_only_unchanged_components(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    original = r'''\documentclass{article}
\usepackage{hyperref}
\begin{document}
\section{Section}\label{sec:first}
Original word. See Section~\ref{sec:first}.
\end{document}
'''
    def compile_at(name, changed=False):
        docs=tmp_path/name; project=docs/'docs-paper'
        (project/'tex/source').mkdir(parents=True)
        for source in ('first','second'):
            text=original.replace('Original', 'Changed' if changed and source=='first' else 'Original')
            (project/f'tex/source/{source}.tex').write_text(text)
        declare(docs, project, 'tex/source/first.tex', 'tex/source/second.tex')
        return [native_renderer._compile_source(docs, project, source+'.tex') for source in ('first','second')]
    compile_at('initial')
    warm=compile_at('warm', True)
    assert not warm[0].compile_cache_hit
    assert warm[1].compile_cache_hit
    monkeypatch.delenv('THOUGHTS_LATEX_CACHE_ROOT')
    cold=compile_at('cold', True)
    for a,b in zip(warm,cold):
        assert [p.extract_text() for p in PdfReader(a.pdf).pages]==[p.extract_text() for p in PdfReader(b.pdf).pages]
        assert str(tmp_path/'initial') not in a.fls.read_text()


@pytest.mark.skipif(not shutil.which('latexmk'), reason='requires TeX Live')
def test_renamed_folder_with_legacy_outputs_reuses_unchanged_components(tmp_path, monkeypatch):
    """docs-concepts-a6fe5d keeps concepts_<name>.pdf, so its build stem is not its source name."""
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    original = r"""\documentclass{article}
\begin{document}
Original word.
\end{document}
"""
    def compile_at(name, changed=False):
        docs=tmp_path/name; project=docs/'docs-concepts-a6fe5d'
        (project/'tex/source').mkdir(parents=True)
        for source in ('first','second'):
            text=original.replace('Original', 'Changed' if changed and source=='first' else 'Original')
            (project/f'tex/source/{source}.tex').write_text(text)
        declare(docs, project, 'tex/source/first.tex', 'tex/source/second.tex',
                outputs={'tex/source/first.tex': 'concepts_first.pdf', 'tex/source/second.tex': 'concepts_second.pdf'})
        return docs, project, [native_renderer._compile_source(docs, project, source+'.tex')
                               for source in ('first','second')]
    compile_at('initial')
    docs, project, warm = compile_at('warm', True)
    assert [build.pdf.name for build in warm] == ['concepts_first.pdf', 'concepts_second.pdf']
    assert [build.fls.stem for build in warm] == ['concepts_first', 'concepts_second']
    assert not warm[0].compile_cache_hit
    assert warm[1].compile_cache_hit
    assert 'docs-concepts-a6fe5d/tex/source/second.tex' in component_cache.authored_dependency_paths(
        docs, project, 'concepts_second', 'tex/source/second.tex')


def test_annotation_scope_does_not_invalidate_compiled_inputs(tmp_path):
    docs=tmp_path/'docs'; project=fixture(docs); cache=tmp_path/'cache.zip'
    comments=docs/'comments';comments.mkdir()
    (comments/'target_comments.jsonl').write_text('annotations')
    component_cache.save(cache,docs,project,'main')
    (comments/'unrelated_comments.jsonl').write_text('unrelated annotations')
    (comments/'.layout').mkdir()
    (comments/'.layout/other.json').write_text('{}')
    details={}
    assert component_cache.restore(cache,docs,project,'main',details)
    (project/'shared.sty').write_text('style changed')
    assert not component_cache.restore(cache,docs,project,'main',details)
    assert details['reason']=='dependency changed: @/docs-paper/shared.sty'


def test_observed_lookup_guards_preserve_other_projects_and_missing_inputs(tmp_path):
    docs = tmp_path/'docs'
    project = fixture(docs)
    cache = tmp_path/'cache.zip'
    output = f'''This is pdfTeX
kdebug:start generic search(files=[main.tex optional.tex], must_exist=1, find_all=0, path={project}//)
kdebug:returning from generic search([main.tex optional.tex]) =>
Output written on main.pdf
'''
    component_cache.save(cache, docs, project, 'main', compiler_output=output)
    other = docs/'docs-other'; other.mkdir()
    (other/'optional.tex').write_text('Unrelated optional input')
    (project/'unrelated-new-file.tex').write_text('Unrelated new file')
    assert component_cache.restore(cache, docs, project, 'main')
    (project/'optional.tex').write_text('Now present')
    details = {}
    assert not component_cache.restore(cache, docs, project, 'main', details)
    assert details['reason'] == 'file lookup changed: @/docs-paper/optional.tex'


@pytest.mark.parametrize('output', [None, '', 'This is pdfTeX\nOutput written on main.pdf',
    'This is pdfTeX\nkdebug:start generic search(unknown syntax)\nOutput written on main.pdf'])
def test_missing_or_unknown_lookup_trace_keeps_conservative_guard(tmp_path, output):
    docs = tmp_path/'docs'; project = fixture(docs); cache = tmp_path/'cache.zip'
    component_cache.save(cache, docs, project, 'main', compiler_output=output)
    other = docs/'docs-other'; other.mkdir(); (other/'new.tex').write_text('new input')
    assert not component_cache.restore(cache, docs, project, 'main')


@pytest.mark.skipif(not shutil.which('latexmk'), reason='requires TeX Live')
def test_real_optional_lookup_add_remove_shadow_and_unrelated_file(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    document = r'''\documentclass{article}
\begin{document}
\def\optionalname{optional}
\IfFileExists{\optionalname.tex}{\input{\optionalname.tex}}{Missing optional input.}
\end{document}
'''
    def build(name, optional=None, unrelated=False, clean=False):
        docs = tmp_path/name; project = docs/'docs-paper'
        (project/'tex/source').mkdir(parents=True)
        (project/'tex/source/main.tex').write_text(document)
        if optional is not None:
            (project/'optional.tex').write_text(optional)
        if unrelated:
            other = docs/'docs-other'; other.mkdir()
            (other/'optional.tex').write_text('Must not affect the first project.')
            (other/'luna_prompt_refinement.tex').write_text('Unrelated addition.')
        declare(docs, project, 'tex/source/main.tex')
        with monkeypatch.context() as scope:
            if clean:
                scope.delenv('THOUGHTS_LATEX_CACHE_ROOT')
            result = native_renderer._compile_source(docs, project, 'main.tex')
        return result
    initial = build('initial')
    unchanged = build('unchanged', unrelated=True)
    assert unchanged.compile_cache_hit
    assert synctex_canonical.canonical_payload(initial.synctex.read_bytes(), tmp_path/'initial') == synctex_canonical.canonical_payload(unchanged.synctex.read_bytes(), tmp_path/'unchanged')
    added = build('added', optional='New optional text.')
    assert not added.compile_cache_hit
    assert 'New optional text.' in PdfReader(added.pdf).pages[0].extract_text()
    warm = build('warm', optional='New optional text.', unrelated=True)
    assert warm.compile_cache_hit
    removed = build('removed')
    assert not removed.compile_cache_hit
    assert 'Missing optional input.' in PdfReader(removed.pdf).pages[0].extract_text()
    for name, cached, optional in [('clean-present', warm, 'New optional text.'), ('clean-absent', removed, None)]:
        clean = build(name, optional=optional, clean=True)
        assert [p.extract_text() for p in PdfReader(cached.pdf).pages] == [p.extract_text() for p in PdfReader(clean.pdf).pages]


@pytest.mark.skipif(not shutil.which('latexmk'), reason='requires TeX Live')
def test_real_shared_lookup_shadowing_and_shared_content_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    document = r'''\documentclass{article}
\usepackage{sharedprobe}
\begin{document}\sharedtext\end{document}
'''
    def compile_at(name, shared='Original shared text.', legacy=False, shadow=False):
        docs=tmp_path/name; project=docs/'docs-paper'
        (project/'tex/source').mkdir(parents=True)
        (project/'tex/source/main.tex').write_text(document)
        support=project/'tex/support';support.mkdir()
        (support/'sharedprobe.sty').write_text(r'\newcommand{\sharedtext}{'+shared+'}')
        if legacy:
            # 59f03c0 retired the workspace tools/latex fallback; it is not a lookup scope.
            common=docs/'tools/latex';common.mkdir(parents=True)
            (common/'sharedprobe.sty').write_text(r'\newcommand{\sharedtext}{Legacy override.}')
        if shadow:
            # The document's own directory is searched before Project support.
            (project/'tex/source/sharedprobe.sty').write_text(r'\newcommand{\sharedtext}{Local override.}')
        declare(docs, project, 'tex/source/main.tex')
        return native_renderer._compile_source(docs,project,'main.tex')
    compile_at('initial')
    assert compile_at('warm').compile_cache_hit
    changed=compile_at('shared-change','Changed shared text.')
    assert not changed.compile_cache_hit
    assert 'Changed shared text.' in PdfReader(changed.pdf).pages[0].extract_text()
    legacy=compile_at('legacy','Changed shared text.',legacy=True)
    assert legacy.compile_cache_hit
    assert 'Changed shared text.' in PdfReader(legacy.pdf).pages[0].extract_text()
    shadowed=compile_at('shadowed','Changed shared text.',shadow=True)
    assert not shadowed.compile_cache_hit
    assert 'Local override.' in PdfReader(shadowed.pdf).pages[0].extract_text()


def test_parent_relative_search_and_custom_compiler_rules_keep_fallback(tmp_path):
    docs=tmp_path/'docs';project=fixture(docs)
    output=f'''This is pdfTeX
kdebug:start generic search(files=[../other/optional.tex], must_exist=1, find_all=0, path={project}//)
kdebug:returning from generic search([../other/optional.tex]) =>
Output written on main.pdf
'''
    assert component_cache.lookup_scopes(output,docs,project,project) is None
    output=output.replace('../other/optional.tex','optional.tex')
    (project/'.latexmkrc').write_text('# custom rules')
    assert component_cache.lookup_scopes(output,docs,project,project) is None


def test_shell_escape_lookup_uses_conservative_inventory(tmp_path):
    docs=tmp_path/'docs';project=fixture(docs);cache=tmp_path/'cache.zip'
    output=f'''This is pdfTeX
kdebug:start generic search(files=[main.tex], must_exist=1, find_all=0, path={project}//)
kdebug:returning from generic search([main.tex]) =>
Output written on main.pdf
'''
    (project/'build/main.log').write_text('runsystem(custom-helper)...executed.\n')
    component_cache.save(cache,docs,project,'main',compiler_output=output)
    (docs/'untracked-by-helper.txt').write_text('Changed shell dependency')
    assert not component_cache.restore(cache,docs,project,'main')


def test_recursive_search_above_workspace_still_guards_workspace_files(tmp_path):
    docs=tmp_path/'docs';project=fixture(docs);cache=tmp_path/'cache.zip'
    output=f'''This is pdfTeX
kdebug:start generic search(files=[optional.tex], must_exist=1, find_all=0, path={tmp_path}//)
kdebug:returning from generic search([optional.tex]) =>
Output written on main.pdf
'''
    component_cache.save(cache,docs,project,'main',compiler_output=output)
    other=docs/'docs-other';other.mkdir();(other/'optional.tex').write_text('Now discoverable')
    assert not component_cache.restore(cache,docs,project,'main')


@pytest.mark.skipif(not shutil.which('latexmk'), reason='requires TeX Live')
def test_review_include_resolution_changes_invalidate_generated_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    document = r'''\documentclass{article}
\providecommand{\agentProposedEdit}[5][]{#5}
\begin{document}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{example}{reason}{}{\input{fragment}}
\end{document}
'''
    def build(name, shadow=False, clean=False):
        docs=tmp_path/name; project=docs/'docs-paper'
        (project/'tex/source').mkdir(parents=True)
        (project/'tex/source/main.tex').write_text(document)
        (project/'tex/fragments').mkdir()
        (project/'tex/fragments/fragment.tex').write_text('Original included sentence.')
        if shadow:
            (project/'fragment.tex').write_text('Higher priority included sentence.')
        declare(docs, project, 'tex/source/main.tex')
        with monkeypatch.context() as scope:
            if clean: scope.delenv('THOUGHTS_LATEX_CACHE_ROOT')
            result=native_renderer._compile_source(docs,project,'main.tex')
        return result
    build('first')
    assert build('warm').compile_cache_hit
    changed=build('changed',shadow=True)
    assert not changed.compile_cache_hit
    clean=build('clean',shadow=True,clean=True)
    assert [p.extract_text() for p in PdfReader(changed.pdf).pages] == [p.extract_text() for p in PdfReader(clean.pdf).pages]
    assert synctex_canonical.canonical_payload(changed.synctex.read_bytes(),tmp_path/'changed') == synctex_canonical.canonical_payload(clean.synctex.read_bytes(),tmp_path/'clean')
