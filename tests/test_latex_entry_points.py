# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""LaTeX entry points anywhere in a connected source folder."""
import shutil
import uuid

import pytest
from pypdf import PdfReader

from thoughts_server import native_renderer as renderer, project_configuration as pc
from thoughts_server.source_inventory import project_authored_source_entries

FILES = {
    'paper/main.tex': '\\documentclass{article}\n\\usepackage{macros}\n\\begin{document}\n\\input{intro}\n\\end{document}\n',
    'paper/intro.tex': 'Paper introduction.\n',
    'paper/figs/plot.png': 'png',
    'paper/data/series.csv': 'x,y\n',
    'build/series.csv': 'x,y\n',
    'shared/macros.sty': '\\ProvidesPackage{macros}\n',
    'intro.tex': 'Root introduction.\n',
    'tex/source/old.tex': 'Old document.\n',
    'tex/generated/table.tex': 'table',
    'build/main.aux': 'aux',
    'build/stale.tex': 'stale',
    '.hidden/draft.tex': 'hidden',
    'private/secret.tex': 'ignored',
}


def project_fixture(tmp_path, documents):
    root = tmp_path / 'docs'
    project = root / 'docs-paper'
    for name, content in FILES.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (project / '.ThoughtIgnore').write_text('private/\n')
    default, folder = str(uuid.uuid4()), str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in documents]
    pc.save(root, {
        'schema_version': 5, 'kind': 'thoughts-project', 'id': 'paper', 'name': 'Paper',
        'default_folder_id': default,
        'source_folders': [
            {'folder_id': default, 'path': pc.default_folder('paper'), 'document_id': str(uuid.uuid4())},
            {'folder_id': folder, 'path': project.name, 'document_id': str(uuid.uuid4())}],
        'documents': [{'document_id': identity, 'format': 'latex', 'included': True,
                       'source': {'folder_id': folder, 'path': source}}
                      for identity, (source, _directory) in zip(ids, documents)],
        'rendering': {identity: {'working_directory': directory}
                      for identity, (_source, directory) in zip(ids, documents) if directory},
    })
    return root, project


def test_entry_point_outside_tex_inventories_its_folder_sources(tmp_path):
    root, project = project_fixture(tmp_path, [('paper/main.tex', 'paper'), ('tex/source/old.tex', None)])
    authored = {e['path'] for e in project_authored_source_entries(root, project)}
    assert authored == {f'docs-paper/{p}' for p in (
        '.ThoughtIgnore', 'paper/main.tex', 'paper/intro.tex', 'shared/macros.sty', 'intro.tex', 'tex/source/old.tex')}


def test_tex_only_project_keeps_its_inventory(tmp_path):
    root, project = project_fixture(tmp_path, [('tex/source/old.tex', None)])
    authored = {e['path'] for e in project_authored_source_entries(root, project)}
    assert authored == {'docs-paper/.ThoughtIgnore', 'docs-paper/tex/source/old.tex'}


@pytest.mark.skipif(not shutil.which('latexmk') or not shutil.which('qpdf'), reason='latexmk and qpdf are required')
def test_entry_point_inputs_resolve_from_its_own_directory(tmp_path):
    root, project = project_fixture(tmp_path, [('paper/main.tex', 'paper')])
    build = renderer._compile_project(root, project, jobs=1)
    assert [s.source_id for s in build.sources] == ['docs-paper/paper/main.tex']
    text = ''.join(page.extract_text() for page in PdfReader(build.sources[0].pdf).pages)
    assert 'Paper introduction.' in text
    assert 'Root introduction.' not in text
