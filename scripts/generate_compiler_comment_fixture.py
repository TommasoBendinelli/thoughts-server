#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Regenerate the shared real-TeX conformance fixture in sibling client checkouts.

Run from thoughts-server with its development virtualenv and TeX/qpdf on PATH:
    .venv/bin/python scripts/generate_compiler_comment_fixture.py
Authored documents, installed apps, and live server state are never touched.
"""
from pathlib import Path
import hashlib,json,shutil,sys,tempfile,os
from thoughts_server.native_renderer import render
repository = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository / 'tests'))
from test_native_renderer import prepare_native_corpus
from test_shipout_placements import CORPUS
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_comments
workspace = tempfile.TemporaryDirectory(prefix='thoughts-compiler-fixture-')
root = Path(workspace.name)
os.environ['THOUGHTS_STRICT_REVIEW_CORRESPONDENCE'] = 'true'
docs,pdfs,artifacts=prepare_native_corpus(root)
source=docs/'docs-manual/tex/source/main.tex'
raw = CORPUS.replace(r'\agentProposedEdit{delete-row}{note}{deleted&row\\}{}', '').encode()
for comment in reversed(parse_review_comments('corpus.tex', raw)):
    if comment['explicit_body'] and comment['target']['byte_end'] > comment['target']['byte_start']:
        at = comment['macro_start']
        raw = raw[:at] + raw[at:].replace(b'\\CommentAnchor', b'\\CommentHighlight', 1)
source.write_bytes(raw)
render(docs,pdfs,artifacts,targets=['manual.pdf'],jobs=1,render_validation=False,prepare_comments=False)
pointer=json.loads((artifacts/'documents/manual.pdf.json').read_text())
artifact=artifacts/'objects'/pointer['artifact_sha256']
manifest=json.loads(artifact.read_text())
assert manifest['review_correspondence_gate']['eligible'],manifest['review_correspondence_gate']
output=repository / 'protocol/fixtures/compiler-comments';output.mkdir(parents=True,exist_ok=True)
for src,name in [(source,'corpus.tex'),(pdfs/'manual.pdf','corpus.pdf'),(artifact,'artifact.json')]:shutil.copyfile(src,output/name)
expected={'schema_version':1,'generator':'tests/test_shipout_placements.py::CORPUS through native_renderer.render',
 'filename':'manual.pdf','pdf_sha256':manifest['pdf']['pdf_sha256'],'artifact_sha256':pointer['artifact_sha256'],
 'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
 'point_pages':{'point-a':1,'point-b':1,'float':3,'last':2},
 'text_comments':['cell','math'], 'proposal':'p1', 'proposal_pages':[1,2,3],
 'required_proposal_text':['Target','C02','question.','answer.','Float','1.','2.'],
 'coincident_points':['point-a','point-b']}
(output/'expectations.json').write_text(json.dumps(expected,indent=2)+'\n')
print('FIXTURE',output,'bytes',artifact.stat().st_size)

# The same bytes are compiled into both client conformance suites.
for client in [
    repository.parent / 'thoughts-desktop/autotests/data/compiler-comments',
    repository.parent / 'thoughts-android/app/src/test/resources/compiler-comments',
    repository.parent / 'thoughts-android/app/src/androidTest/assets/compiler-comments',
]:
    if not client.parent.is_dir():
        raise SystemExit(f'Missing client checkout: {client.parent}')
    client.mkdir(exist_ok=True)
    for name in ('corpus.tex', 'corpus.pdf', 'artifact.json', 'expectations.json'):
        shutil.copyfile(output / name, client / name)
workspace.cleanup()
