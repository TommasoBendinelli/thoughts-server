# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import gzip
import io
import zipfile
from pathlib import Path

from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher


def test_analysis_reuses_canonical_maps_across_workspaces_and_invalidates_queries(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    worker = tmp_path/'worker';worker.write_bytes(b'worker-version-one')
    calls=[]
    def publisher(name):
        docs=tmp_path/name;docs.mkdir()
        p=object.__new__(PhoneSourceBundlePublisher)
        p.docs_root=docs;p.native_worker=worker
        source='docs-paper/tex/source/main.tex'
        inputs=[{'source_file':source,'source_sha256':'a'*64,'synctex_path':str(docs/source)}]
        synctex=docs/'main.synctex.gz'
        synctex.write_bytes(gzip.compress(f'SyncTeX Version:1\nInput:1:{docs/source}\n!12\n'.encode()))
        queries=[('proposal','after',inputs[0],{'line_start':1,'line_end':2})]
        def analyze(*args):
            calls.append(name)
            data=io.BytesIO()
            with zipfile.ZipFile(data,'w') as archive:
                archive.writestr('pages/1.tsv',str(docs/source))
            return data.getvalue(), {('proposal','after'):{1:[(1.,2.,3.,4.)]}}
        monkeypatch.setattr(p,'_native_analyze_synctex_uncached',analyze)
        return p,synctex,inputs,queries
    first=publisher('first')
    expected=first[0]._native_analyze_synctex(*first[1:])
    second=publisher('longer-second-root')
    actual=second[0]._native_analyze_synctex(*second[1:])
    assert actual==expected
    assert calls==['first']
    with zipfile.ZipFile(io.BytesIO(actual[0])) as archive:
        assert archive.read('pages/1.tsv')==b'docs-paper/tex/source/main.tex'
    second[3][0][3]['line_end']=3
    second[0]._native_analyze_synctex(*second[1:])
    assert len(calls)==2
    second[2][0]['source_sha256']='b'*64
    second[0]._native_analyze_synctex(*second[1:])
    assert len(calls)==3
    worker.write_bytes(b'new-worker-version')
    second[0]._native_analyze_synctex(*second[1:])
    assert len(calls)==4
    next((tmp_path/'cache/source-analysis').glob('*.zip')).write_bytes(b'corrupt cache')
    second[0]._native_analyze_synctex(*second[1:])
    assert len(calls)==5
