# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid
import json
from pathlib import Path

import pytest

from thoughts_server import legacy_project_manifest_migration as pm
from thoughts_server.migrate_project_manifests import migrate


def write(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value) if isinstance(value, dict) else value)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'docs-example'
    write(root, 'tex/source/a.tex', 'A')
    write(root, 'tex/source/b.tex', 'B')
    write(root, pm.LEGACY_ORDER, 'b.tex\na.tex\n')
    write(root, pm.OPTIONS, {'schema_version': 1, 'selected_documents': ['docs-example/tex/source/a.tex'],
                            'titles': {'docs-example/tex/source/a.tex': 'Custom title'}, 'excluded_documents': []})
    write(root, pm.ATTACHMENTS, {'schema_version': 1, 'attachments': [
        {'filename': 'paper.pdf', 'sha256': 'a'*64, 'title': 'Paper', 'placement': 'append'}]})
    write(root, pm.ORDER, {'schema_version': 1, 'documents': ['third_party/paper.pdf', 'docs-example/tex/source/b.tex', 'docs-example/tex/source/a.tex']})
    return root


def test_migration_preserves_effective_selection_order_titles_hashes_and_backups(project):
    old = {name: (project / name).read_bytes() for name in pm.LEGACY_PATHS}
    expected = pm.load(project)
    preview = migrate(project.parent)
    assert preview['manifests'] == ['docs-example/thoughts-project.json']
    assert not (project / pm.MANIFEST).exists()
    report = migrate(project.parent, apply=True)
    assert pm.load(project) == expected
    assert [d['included'] for d in expected['documents']] == [True, False, True]
    assert expected['documents'][0]['sha256'] == 'a'*64
    for path, raw in old.items():
        assert not (project / path).exists()
        assert (Path(report['backup']) / project.name / path).read_bytes() == raw
    assert migrate(project.parent, apply=True)['manifests'] == []
    # Stale clients cannot override v2 by restoring a retired settings file.
    write(project, pm.OPTIONS, {'schema_version': 1, 'selected_documents': ['docs-example/tex/source/b.tex']})
    assert pm.load(project) == expected


def test_migrated_manifest_does_not_include_new_sources_implicitly(project):
    migrate(project.parent, apply=True)
    write(project, 'tex/source/new.tex', 'New')
    assert pm.options(pm.load(project))['selected_documents'] == ['docs-example/tex/source/a.tex']


def test_v3_source_manifest_uses_permanent_id_and_source_without_path_id_or_title(project):
    aggregate = str(uuid.uuid4())
    document_id = str(uuid.uuid4())
    value = {'schema_version': 3, 'document_id': aggregate, 'documents': [
        {'document_id': document_id, 'source': 'tex/source/a.tex', 'format': 'latex', 'included': True},
    ]}
    write(project, pm.MANIFEST, value)
    loaded = pm.load(project)
    assert loaded['documents'][0]['id'] == 'docs-example/tex/source/a.tex'
    assert pm.for_storage(loaded) == value
    assert pm.options(pm.load(project))['selected_documents'] == ['docs-example/tex/source/a.tex']
    assert pm.options(loaded)['selected_documents'] == ['docs-example/tex/source/a.tex']
    assert loaded['documents'][0]['document_id'] == document_id
    for extra in ({'id': 'docs-example/tex/source/a.tex'}, {'title': 'A'}):
        write(project, pm.MANIFEST, {**value, 'documents': [{**value['documents'][0], **extra}]})
        with pytest.raises(ValueError):
            pm.load(project)


def test_upgrade_keeps_legacy_names_and_custom_source_keys(project):
    value = pm.load(project)
    value['document_id'] = str(uuid.uuid4())
    for entry in value['documents']:
        if entry['format'] in {'latex', 'markdown'}:
            entry['document_id'] = str(uuid.uuid4())
    assert pm.upgrade_when_safe(value, project.name)['schema_version'] == 2
    for entry in value['documents']:
        entry.pop('title', None)
    upgraded = pm.upgrade_when_safe(value, project.name)
    assert upgraded['schema_version'] == 3
    assert all('id' not in d for d in upgraded['documents'] if d['format'] in {'latex', 'markdown'})
    assert pm.order(pm.with_runtime_ids(upgraded, project.name)) == pm.order(value)


def test_v1_markdown_retains_explicit_identity_output_stylesheet_and_disabled_candidates(tmp_path):
    project = tmp_path / 'docs-markdown'
    write(project, 'markdown/source/first.md', '# First')
    write(project, 'markdown/source/second.md', '# Second')
    write(project, 'markdown/support/style.css', 'body {}')
    definition = {'source': 'markdown/source/first.md', 'format': 'markdown', 'id': 'docs-markdown/markdown/source/stable.md', 'output': 'custom.pdf', 'title': 'First', 'stylesheet': 'markdown/support/style.css'}
    write(project, pm.MANIFEST, {'schema_version': 1, 'documents': [definition]})
    migrate(tmp_path, apply=True)
    manifest = pm.load(project)
    assert manifest['documents'][0] == {**definition, 'included': True}
    assert manifest['documents'][1]['included'] is False


def test_settings_mutations_preserve_disabled_documents_and_pdf_order(project):
    value = pm.load(project)
    value = pm.apply_legacy(value, pm.ORDER, {'documents': ['docs-other/markdown/source/foreign.md', 'docs-example/tex/source/a.tex', 'third_party/paper.pdf', 'docs-example/tex/source/b.tex']})
    assert value['documents'][0]['format'] == 'reference'
    assert value['documents'][-1]['included'] is False
    assert value['documents'][2]['sha256'] == 'a'*64
    attachment_update = pm.attachments(value, include_disabled=True)
    attachment_update['attachments'].append({'filename':'other.pdf','sha256':'b'*64,'title':'Other','placement':'append'})
    value = pm.apply_legacy(value, pm.ATTACHMENTS, attachment_update)
    assert pm.order(value)[-1] == 'third_party/other.pdf'
    assert value['documents'][2]['sha256'] == 'a'*64
    pm.validate(value, project.name)


@pytest.mark.parametrize('change', [
    lambda d: d.update(included='false'),
    lambda d: d.update(id='../outside'),
    lambda d: d.update(source='tex/source/../../outside.tex'),
    lambda d: d.update(format='unknown'),
])
def test_invalid_v2_is_rejected_without_falling_back_to_legacy(project, change):
    value = pm.load(project)
    change(value['documents'][1])
    write(project, pm.MANIFEST, value)
    with pytest.raises(ValueError):
        pm.load(project)


def test_canonical_migration_records_manifest_and_retires_legacy_sources(project, settings):
    from thoughts_server.database import Database
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.legacy_project_manifest_migration import save_source as _save_source
    from thoughts_server.models import SourceFile
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        # Simulate a source record enrolled before migration, without modern writers.
        for path in pm.LEGACY_PATHS:
            raw = (project / path).read_bytes()
            obj = store.put_bytes(session, raw, 'text/plain')
            session.add(SourceFile(path=project.name+'/'+path, revision=1, sha256=obj.sha256, object_sha256=obj.sha256, deleted=False, operation_id="legacy-import"))
        session.commit()
        expected = pm.load(project)
        migrate(project.parent, apply=True, session=session, store=store)
        actual = pm.load_canonical(session, store, project.parent, project.name)
        assert uuid.UUID(actual.pop("document_id")).version == 4
        for entry in actual["documents"]:
            if "document_id" in entry:
                assert uuid.UUID(entry.pop("document_id")).version == 4
        assert actual == expected
        for path in pm.LEGACY_PATHS:
            assert session.get(SourceFile, project.name+'/'+path).deleted
        assert migrate(project.parent, apply=True, session=session, store=store)['manifests'] == []
        # A PDF move updates several settings in one transaction before materialization.
        _save_source(session, store, project.parent, 'move-operation', project.name+'/'+pm.ATTACHMENTS,
                     json.dumps({'attachments': []}).encode(), materialize=False)
        _save_source(session, store, project.parent, 'move-operation', project.name+'/'+pm.ORDER,
                     json.dumps({'documents': ['docs-example/tex/source/a.tex']}).encode(), materialize=False)
        current = pm.load_canonical(session, store, project.parent, project.name)
        assert not any(d['format'] == 'pdf' for d in current['documents'])
        assert current['documents'][-1]['included'] is False


def test_migration_keeps_library_order_across_folders_and_linked_pdfs(project):
    (project / pm.ORDER).unlink()
    membership = {'schema_version': 1, 'kind': 'thoughts-library-project', 'id': 'source:docs-example',
                  'source_project': project.name, 'source_folders': [{'project': 'docs-other'}],
                  'documents': [{'id': identity} for identity in ['docs-other/markdown/source/one.md',
                      'docs-example/tex/source/a.tex', 'third_party/linked.pdf', 'third_party/paper.pdf']]}
    write(project.parent, '.thoughts/projects/current/thoughts-project.json', membership)
    migrate(project.parent, apply=True)
    value = pm.load(project)
    enabled = [d['id'] for d in value['documents'] if d['included']]
    assert enabled == [d['id'] for d in membership['documents']]
    assert value['documents'][0]['id'] == 'docs-example/tex/source/b.tex'
    assert value['documents'][0]['included'] is False
    assert migrate(project.parent, apply=True)['manifests'] == []


def test_migration_keeps_unsynced_source_order_over_older_library_order(project):
    write(project, pm.ORDER, {'schema_version': 1, 'documents': ['third_party/paper.pdf', 'docs-example/tex/source/a.tex', 'docs-example/tex/source/b.tex']})
    membership = {'schema_version': 1, 'kind': 'thoughts-library-project', 'source_project': project.name,
                  'documents': [{'id': 'docs-example/tex/source/a.tex'}, {'id': 'third_party/paper.pdf'}]}
    write(project.parent, '.thoughts/projects/current/thoughts-project.json', membership)
    migrate(project.parent, apply=True)
    assert pm.order(pm.load(project)) == ['third_party/paper.pdf', 'docs-example/tex/source/a.tex', 'docs-example/tex/source/b.tex']
