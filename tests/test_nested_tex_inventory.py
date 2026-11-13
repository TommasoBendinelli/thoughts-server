# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid

from thoughts_server.project_configuration import default_folder, save
from thoughts_server.source_inventory import project_authored_source_entries, project_tex_roots


def test_declared_nested_tex_tree_is_inventoried_without_following_aliases(tmp_path):
    project = tmp_path / 'docs-nested'
    project.mkdir()
    default, folder, document = (str(uuid.uuid4()) for _ in range(3))
    project_id = 'source:docs-nested'
    save(tmp_path, {
        'schema_version': 5, 'kind': 'thoughts-project', 'id': project_id, 'name': 'Nested',
        'default_folder_id': default,
        'source_folders': [
            {'folder_id': default, 'path': default_folder(project_id), 'document_id': str(uuid.uuid4())},
            {'folder_id': folder, 'path': project.name, 'document_id': str(uuid.uuid4())},
        ],
        'documents': [{'document_id': document, 'format': 'latex', 'included': True,
                       'source': {'folder_id': folder, 'path': 'overleaf/tex/source/main.tex'}}],
        'rendering': {document: {'working_directory': 'overleaf'}},
    })
    files = {
        'overleaf/tex/source/main.tex': b'example',
        'overleaf/tex/support/style.sty': b'style',
        'overleaf/tex/generated/table.tex': b'table',
        'overleaf/tex/source/figure.pdf': b'figure',
        'overleaf/tex/source/ignored.tex': b'ignored',
        'overleaf/archive/main.tex': b'archived',
    }
    for name, content in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (project / '.ThoughtIgnore').write_text('overleaf/tex/source/ignored.tex\n')
    (project / 'tex').mkdir()
    (project / 'tex/source').symlink_to('../overleaf/tex/source')
    (project / 'overleaf/tex/source/alias.tex').symlink_to('main.tex')
    (project / 'overleaf/tex/generated/alias.tex').symlink_to('table.tex')
    authored = {e['path'] for e in project_authored_source_entries(tmp_path, project)}
    assert authored == {f'{project.name}/{p}' for p in (
        '.ThoughtIgnore', 'overleaf/tex/source/main.tex', 'overleaf/tex/support/style.sty')}
    # A linked parent cannot bypass the no-links rule for an entire declared tree.
    (project / 'overleaf').rename(project / 'actual')
    (project / 'overleaf').symlink_to('actual')
    assert project_tex_roots(project) == [project / 'tex']
    assert {e['path'] for e in project_authored_source_entries(tmp_path, project)} == {
        project.name + '/.ThoughtIgnore'}
