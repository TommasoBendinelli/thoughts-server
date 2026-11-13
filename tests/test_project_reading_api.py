# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import io
import json
import uuid
from pypdf import PdfWriter
from thoughts_server.models import Publication, DocumentIdentity
from thoughts_server.project_reading import ProjectReading, ReadingDocument, publish_reading
from thoughts_server.project_exports import ProjectExportWorker
from thoughts_server.library import store_published_source_index


def test_sync_plan_delivers_reading_closure_and_export_is_explicit(client, auth_headers, tmp_path):
    database, store = client.app.state.database, client.app.state.object_store
    documents=[]
    with database.session_factory() as session:
        for name in ['one','two']:
            stream=io.BytesIO()
            with PdfWriter() as writer:
                writer.add_blank_page(width=200,height=300);writer.write(stream)
            pdf=store.put_bytes(session,stream.getvalue(),'application/pdf')
            identity=str(uuid.uuid4())
            session.add(DocumentIdentity(document_id=identity,filename=name+'.pdf'))
            session.add(Publication(filename=name+'.pdf',document_id=identity,object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,metadata_json={'kind':'unlinked','page_count':1}))
            pack=store.put_bytes(session,json.dumps({'schema_version':1,'kind':'thoughts-document-reading-package',
                'document_id':identity,'filename':name+'.pdf','pdf_sha256':pdf.sha256,'page_count':1}).encode(),'application/json')
            documents.append(ReadingDocument(identity,name,pdf.sha256,pdf.size_bytes,1,pack.sha256))
        reading=ProjectReading('course','Course',tuple(documents))
        publish_reading(session,store,reading)
        store_published_source_index(session,store)
        session.commit()
    plan=client.post('/v2/sync/plan',headers=auth_headers,json={'source_profile':'reader-v1',
        'capabilities':['project_reading_v1','source_resource_v1'],'known_object_sha256s':[]})
    assert plan.status_code==200,plan.text
    payload=plan.json()
    assert reading.revision in json.dumps(payload)
    for document in documents:
        assert document.package_sha256 in json.dumps(payload)
        assert document.pdf_sha256 in json.dumps(payload)
    denied=client.post('/v2/sync/plan',headers=auth_headers,json={'source_profile':'reader-v1','capabilities':[]})
    assert denied.status_code==426
    identity=str(uuid.uuid4())
    request={'request_id':identity,'project_id':reading.project_id,'reading_revision':reading.revision}
    created=client.post('/v2/library/project-exports',headers=auth_headers,json=request)
    assert created.status_code==200,created.text
    assert created.json()['status']=='queued'
    assert ProjectExportWorker(database.session_factory,store,tmp_path/'exports').run_once()
    status=client.get('/v2/library/project-exports/'+identity,headers=auth_headers).json()
    assert status['status']=='complete',status
    downloaded=client.get('/v2/library/project-exports/'+identity+'/pdf',headers=auth_headers)
    assert downloaded.status_code==200,downloaded.text
    assert hashlib.sha256(downloaded.content).hexdigest()==status['sha256']
    retry=client.post('/v2/library/project-exports',headers=auth_headers,json=request)
    assert retry.json()['id']==identity and retry.json()['status']=='complete'
