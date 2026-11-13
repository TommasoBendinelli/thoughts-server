# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid

import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from test_library_projects import library, add_pdf, operation
from thoughts_server.library_projects import mutate, catalogue, repair_project_readings
from thoughts_server.models import ReadingPublication, Publication, ProjectExport
from thoughts_server.project_reading import load_reading
from thoughts_server.project_exports import request_export, ProjectExportWorker, export_policy, export_status


def test_reading_reorder_and_export_are_independent(library):
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100, 110])
    b = add_pdf(library, "b.pdf", [200])
    mutate(*library, operation("create", name="Course"))
    mutate(*library, operation("import", base_revision=1, filename="a.pdf", sha256=a.sha256, name="First"))
    result = mutate(*library, operation("import", base_revision=2, filename="b.pdf", sha256=b.sha256, name="Second"))
    project = result["projects"][0]
    reading = load_reading(store, project["reading_revision"])
    assert [d.page_count for d in reading.documents] == [2, 1]
    assert [d['start_page'] for d in project['documents']] == [1, 1]
    assert [d['project_start_page'] for d in project['documents']] == [1, 3]
    assert project['target'].endswith('.thoughts-project')
    assert set(session.scalars(select(Publication.filename))) == {'a.pdf', 'b.pdf'}
    assert list(session.scalars(select(ProjectExport))) == []
    assert repair_project_readings(*library) == 0
    request = dict(request_id=str(uuid.uuid4()), project_id=reading.project_id, revision=reading.revision, policy=export_policy('test'))
    export = request_export(session, store, **request)
    session.commit()
    assert request_export(session, store, **request).id == export.id
    with pytest.raises(ValueError):
        request_export(session, store, **{**request, 'policy':'different'})
    reordered = mutate(*library, operation('order', base_revision=3,
        documents=[d.document_id for d in reversed(reading.documents)]))['projects'][0]
    assert reordered['reading_revision'] != reading.revision
    assert [d.pdf_sha256 for d in load_reading(store, reordered['reading_revision']).documents] == [b.sha256, a.sha256]
    worker = ProjectExportWorker(sessionmaker(bind=session.get_bind()), store, root / 'exports')
    assert worker.run_once()
    session.expire_all()
    assert export.status == 'complete', export.error
    assert export_status(export, reordered['reading_revision'])['older_revision']
    with PdfReader(store.path(export.object_sha256)) as pdf:
        assert [int(p.mediabox.width) for p in pdf.pages] == [100, 110, 200]
        assert [item.title for item in pdf.outline] == ['First', 'Second']
    assert not worker.run_once()
    reused = request_export(session, store, **{**request, 'request_id':str(uuid.uuid4())})
    assert reused.status == 'complete' and reused.object_sha256 == export.object_sha256
    mutate(*library, operation('remove', base_revision=4))
    assert session.get(ReadingPublication, reading.project_id) is None
    assert load_reading(store, reading.revision) == reading
    assert store.matches(export.object_sha256, export.size_bytes)


def test_reading_skips_a_document_without_a_matching_publication(library):
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100, 110])
    b = add_pdf(library, "b.pdf", [200])
    mutate(*library, operation("create", name="Course"))
    mutate(*library, operation("import", base_revision=1, filename="a.pdf", sha256=a.sha256, name="First"))
    project = mutate(*library, operation("import", base_revision=2, filename="b.pdf", sha256=b.sha256, name="Second"))["projects"][0]
    # A declared document whose PDF was never published used to withhold the whole Project.
    session.delete(session.get(Publication, "b.pdf"))
    session.delete(session.get(ReadingPublication, project["id"]))
    session.commit()
    assert repair_project_readings(*library) == 1
    project = catalogue(session, root, store)["projects"][0]
    assert project["target"].endswith(".thoughts-project")
    assert [d.pdf_sha256 for d in load_reading(store, project["reading_revision"]).documents] == [a.sha256]
    assert [d["name"] for d in project["documents"]] == ["First", "Second"]


def test_reading_follows_the_documents_current_publication_over_a_stale_pin(library):
    from io import BytesIO
    from pypdf import PdfWriter
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100])
    b = add_pdf(library, "b.pdf", [200])
    mutate(*library, operation("create", name="Course"))
    mutate(*library, operation("import", base_revision=1, filename="a.pdf", sha256=a.sha256, name="First"))
    project = mutate(*library, operation("import", base_revision=2, filename="b.pdf", sha256=b.sha256, name="Second"))["projects"][0]
    # The same document re-saved: its publication changes, the Project still pins the older copy.
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=210, height=300)
    writer.write(output)
    resaved = store.put_bytes(session, output.getvalue(), "application/pdf")
    publication = session.get(Publication, "b.pdf")
    publication.object_sha256, publication.size_bytes = resaved.sha256, resaved.size_bytes
    session.delete(session.get(ReadingPublication, project["id"]))
    session.commit()
    assert repair_project_readings(*library) == 1
    project = catalogue(session, root, store)["projects"][0]
    assert not any(d.get("pending_build") for d in project["documents"])
    assert [d.pdf_sha256 for d in load_reading(store, project["reading_revision"]).documents] == [a.sha256, resaved.sha256]


def publish_project_file(library, project_id, change):
    """Publish an edited Project file the way a device's Sync does."""
    import hashlib
    import json
    from thoughts_server.models import Device, SourceFile
    from thoughts_server.operations import latest_device_sequence
    from thoughts_server.project_configuration import manifest_path
    from thoughts_server.schemas import SourcePublicationMutation, SourcePublicationRequest
    from thoughts_server.sources import publish_sources
    session, store, root = library
    if session.get(Device, "desktop") is None:
        session.add(Device(id="desktop", name="Desktop", token_hash=hashlib.sha256(b"desktop").hexdigest()))
        session.commit()
    path = manifest_path(project_id)
    current = session.get(SourceFile, path)
    value = json.loads(store.path(current.object_sha256).read_bytes())
    change(value)
    incoming = store.put_bytes(session, (json.dumps(value, indent=2) + "\n").encode(), "text/plain")
    session.commit()
    publish_sources(session, store, root, "desktop", SourcePublicationRequest(
        publication_id=str(uuid.uuid4()), mutations=[SourcePublicationMutation(
            action="upsert", operation_id=str(uuid.uuid4()),
            device_sequence=latest_device_sequence(session, "desktop") + 1, path=path,
            base_revision=current.revision, base_sha256=current.sha256, base_object_sha256=current.object_sha256,
            content_sha256=incoming.sha256, client_timestamp="2026-10-01T00:00:00Z")]))


def test_project_file_publication_republishes_its_reading(library):
    """Only builds and Library operations republished readings.

    Excluding a document is a Project-file publication that is neither, so the
    reading kept listing it.
    """
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100])
    b = add_pdf(library, "b.pdf", [200])
    mutate(*library, operation("create", name="Course"))
    mutate(*library, operation("import", base_revision=1, filename="a.pdf", sha256=a.sha256, name="First"))
    project = mutate(*library, operation("import", base_revision=2, filename="b.pdf", sha256=b.sha256, name="Second"))["projects"][0]
    assert [d.title for d in load_reading(store, project["reading_revision"]).documents] == ["First", "Second"]
    second = next(d["document_id"] for d in project["documents"] if d["filename"] == "b.pdf")

    def include(value):
        def change(configuration):
            next(d for d in configuration["documents"] if d["document_id"] == second)["included"] = value
        return change

    publish_project_file(library, project["id"], include(False))
    project = catalogue(session, root, store)["projects"][0]
    assert [d.title for d in load_reading(store, project["reading_revision"]).documents] == ["First"]
    publish_project_file(library, project["id"], include(True))
    project = catalogue(session, root, store)["projects"][0]
    assert [d.title for d in load_reading(store, project["reading_revision"]).documents] == ["First", "Second"]


def test_export_contains_only_the_chosen_documents(library):
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100, 110])
    b = add_pdf(library, "b.pdf", [200])
    c = add_pdf(library, "c.pdf", [300])
    mutate(*library, operation("create", name="Course"))
    mutate(*library, operation("import", base_revision=1, filename="a.pdf", sha256=a.sha256, name="First"))
    mutate(*library, operation("import", base_revision=2, filename="b.pdf", sha256=b.sha256, name="Second"))
    project = mutate(*library, operation("import", base_revision=3, filename="c.pdf", sha256=c.sha256, name="Third"))["projects"][0]
    reading = load_reading(store, project["reading_revision"])
    first, second, third = (d.document_id for d in reading.documents)
    request = dict(project_id=reading.project_id, revision=reading.revision, policy=export_policy('test'))
    with pytest.raises(ValueError):
        request_export(session, store, request_id=str(uuid.uuid4()), document_ids=[], **request)
    with pytest.raises(ValueError):
        request_export(session, store, request_id=str(uuid.uuid4()), document_ids=[str(uuid.uuid4())], **request)
    # Chosen out of order, exported in reading order.
    chosen = request_export(session, store, request_id=str(uuid.uuid4()), document_ids=[third, first], **request)
    full = request_export(session, store, request_id=str(uuid.uuid4()), **request)
    everything = request_export(session, store, request_id=str(uuid.uuid4()), document_ids=[second, third, first], **request)
    session.commit()
    assert chosen.document_ids == [first, third]
    assert full.document_ids is None and everything.document_ids is None
    with pytest.raises(ValueError):
        request_export(session, store, request_id=chosen.id, **request)
    assert request_export(session, store, request_id=chosen.id, document_ids=[first, third], **request).id == chosen.id
    worker = ProjectExportWorker(sessionmaker(bind=session.get_bind()), store, root / 'exports')
    while worker.run_once():
        pass
    session.expire_all()
    assert chosen.status == 'complete', chosen.error
    assert export_status(chosen, reading.revision)['document_ids'] == [first, third]
    with PdfReader(store.path(chosen.object_sha256)) as pdf:
        assert [int(p.mediabox.width) for p in pdf.pages] == [100, 110, 300]
        assert [item.title for item in pdf.outline] == ['First', 'Third']
    with PdfReader(store.path(full.object_sha256)) as pdf:
        assert len(pdf.pages) == 4
    reused = request_export(session, store, request_id=str(uuid.uuid4()), document_ids=[first, third], **request)
    assert reused.status == 'complete' and reused.object_sha256 == chosen.object_sha256
    assert request_export(session, store, request_id=str(uuid.uuid4()), document_ids=[second], **request).status == 'queued'
