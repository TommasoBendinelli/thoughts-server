# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import json
import uuid
from sqlalchemy.orm import Session
from thoughts_server.builds import _unbuilt_source_projects
from thoughts_server.database import Database
from thoughts_server.models import Publication, DocumentIdentity
from thoughts_server.object_store import ObjectStore
from thoughts_server import project_configuration as pc


def test_build_discovery_selects_documents_not_folder_pdfs(tmp_path, monkeypatch):
    db = Database('sqlite:///' + str(tmp_path / 'state.db'))
    db.create_schema()
    store = ObjectStore(tmp_path / 'objects')
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    configuration = {'documents': [
        {'document_id': a, 'included': True, 'format': 'latex', 'source': {'folder_id':'folder','path':'tex/source/a.tex'}},
        {'document_id': b, 'included': True, 'format': 'markdown', 'source': {'folder_id':'folder','path':'notes/b.md'}}],
        'rendering': {b:{'output':'custom.pdf'}}}
    monkeypatch.setattr(pc, 'canonical_projects', lambda session, objects: [configuration])
    monkeypatch.setattr(pc, 'resolve', lambda config, entry: 'docs-course/' + entry['source']['path'])
    with db.session_factory() as session:
        assert _unbuilt_source_projects(session, store) == {'course_a.pdf':'docs-course', 'custom.pdf':'docs-course'}
        pdf = store.put_bytes(session, b'fixture', 'application/pdf')
        session.add(DocumentIdentity(document_id=a,filename='course_a.pdf',source_path='docs-course/tex/source/a.tex'))
        session.add(Publication(filename='course_a.pdf',document_id=a,object_sha256=pdf.sha256,size_bytes=pdf.size_bytes,project='docs-course'))
        session.flush()
        assert _unbuilt_source_projects(session, store) == {'custom.pdf':'docs-course'}


def test_binary_cleanup_preserves_canonical_generated_sources(tmp_path):
    from thoughts_server.models import SourceFile, BuildInputSnapshot, Device
    from thoughts_server.build_inputs import materialize_build_input_snapshots
    db=Database('sqlite:///'+str(tmp_path/'state.db'));db.create_schema()
    store=ObjectStore(tmp_path/'objects');root=tmp_path/'worktree'
    relative='docs-course/tex/generated/example.txt';source=root/relative
    source.parent.mkdir(parents=True);source.write_bytes(b'canonical source')
    state=root/'.runtime/server-build-inputs/active.json';state.parent.mkdir(parents=True)
    state.write_text(json.dumps({'paths':[relative]}))
    with db.session_factory() as s:
        s.add(Device(id='test',name='test',token_hash='0'*64));s.flush()
        record=store.put_bytes(s,source.read_bytes(),'text/plain')
        s.add(SourceFile(path=relative,revision=1,sha256=record.sha256,object_sha256=record.sha256,operation_id='test'))
        manifest=store.put_bytes(s,b'{}','application/json')
        snapshot=BuildInputSnapshot(sha256=manifest.sha256,schema_version=3,target='course_a.pdf',project='docs-course',discovery_policy='source-closure-v2',manifest_object_sha256=manifest.sha256,entries_json=[],created_by='test')
        s.add(snapshot);s.flush()
        assert materialize_build_input_snapshots(s,store,root,[snapshot.sha256])==0
        assert source.read_bytes()==b'canonical source'


def test_document_input_migration_reuses_verified_snapshot_once(tmp_path, monkeypatch):
    from thoughts_server import builds, library_projects
    from thoughts_server.models import Device, BuildInputSnapshot
    from thoughts_server.build_inputs import latest_build_input_snapshot
    from thoughts_server.reading_migration import migrate_document_build_inputs
    db=Database('sqlite:///'+str(tmp_path/'state.db'));db.create_schema();store=ObjectStore(tmp_path/'objects')
    monkeypatch.setattr(builds,'_unbuilt_source_projects',lambda *args:{'course_a.pdf':'docs-course'})
    monkeypatch.setattr(library_projects,'retired_filenames',lambda session:{'course.pdf'})
    with db.session_factory() as s:
        s.add(Device(id='test',name='test',token_hash='0'*64));s.flush()
        obj=store.put_bytes(s,b'asset','application/octet-stream');manifest=store.put_bytes(s,b'{}','application/json')
        entries=[{'path':'docs-course/tex/assets/image.bin','sha256':obj.sha256,'size_bytes':5,'media_type':'application/octet-stream','role':'asset'}]
        s.add(Publication(filename='course_a.pdf',project='docs-course',object_sha256=obj.sha256,size_bytes=5))
        s.add(BuildInputSnapshot(sha256=manifest.sha256,schema_version=3,target='course.pdf',project='docs-course',discovery_policy='source-closure-v2',manifest_object_sha256=manifest.sha256,entries_json=entries,created_by='test'));s.flush()
        assert migrate_document_build_inputs(s,store,'test')==1
        assert latest_build_input_snapshot(s,'course_a.pdf').entries_json==entries
        assert migrate_document_build_inputs(s,store,'test')==0


def test_running_global_build_checks_shared_identity_once(monkeypatch):
    from types import SimpleNamespace
    from thoughts_server import builds
    active = SimpleNamespace(id="active", target="all", status="running")
    session = SimpleNamespace(get=lambda *args: active)
    documents = [{"filename": name, "state": "building", "reason": "running", "build": {"id": "active"}}
                 for name in ["a.pdf", "b.pdf"]]
    monkeypatch.setattr(builds, "_frontier", lambda *args: 42)
    monkeypatch.setattr(builds, "_current_input_snapshot_identity", lambda *args: "inputs")
    monkeypatch.setattr(builds, "_aggregate_linked_render_status", lambda *args: {
        "status": "building", "build": {"id": "active"}, "documents": documents})
    monkeypatch.setattr(builds, "build_render_fingerprint", lambda *args: "current")
    fingerprint_calls = []
    monkeypatch.setattr(builds, "_job_render_fingerprint", lambda *args: fingerprint_calls.append(1) or "current")
    changed_calls = []
    monkeypatch.setattr(builds, "_building_document_changed", lambda *args: changed_calls.append(args[1]) or False)
    assert builds.conditional_render_check(session, "test", "all", "toolchain")["status"] == "building"
    assert len(fingerprint_calls) == 1 and not changed_calls
    # A changed global identity must still inspect individual document inputs.
    monkeypatch.setattr(builds, "_job_render_fingerprint", lambda *args: "old")
    builds.conditional_render_check(session, "test", "all", "toolchain")
    assert changed_calls == ["a.pdf", "b.pdf"]
