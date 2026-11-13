# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A rejected placement must explain itself without admitting invalid geometry."""
import hashlib
import io
import json
from types import SimpleNamespace

import pdfplumber
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    PhoneSourceBundlePublisher, parse_agent_proposals, tex_literal_text,
)
from thoughts_server.renderer_tools.review_source_staging import instrument_source
from thoughts_server.renderer_tools.shipout_placements import (
    compiler_segment_locations, shipout_markers,
)
from thoughts_server.review_correspondence import strict_gate_failures

RAW = b'\\begin{document}\n\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{caption}{Reason}{Old}{Additional}\n\\end{document}'
SOURCE = 'docs-paper/tex/source/main.tex'
KEY = 'proposal:caption'


def marker_pdf(x=20, y=-19.35, duplicate=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    content = f'q 1 0 0 1 {x} {y} cm\n'
    for role in ['span-0-start', 'span-0-end']:
        marker = f'/thoughts-shipout:{KEY}:{role}:2 BMC 0 0 m n EMC\n'
        content += marker * (2 if duplicate else 1)
    content += 'Q\n'
    stream = DecodedStreamObject()
    stream.set_data(content.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize('x,y,edge', [(20, -19.35, 'bottom'), (-12, 100, 'left'), (20, 210, 'top'), (215, 100, 'right')])
def test_outside_page_retains_rejection_evidence_without_accepting_marker(x, y, edge):
    pdf = marker_pdf(x, y)
    reader = PdfReader(io.BytesIO(pdf))
    assert KEY not in shipout_markers(reader)
    _, report = instrument_source(RAW)
    diagnostics = []
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        assert compiler_segment_locations(reader, document, RAW, report, KEY, tex_literal_text,
                                          diagnostics=diagnostics) is None
    assert diagnostics[0]['code'] == 'marker_outside_page'
    assert diagnostics[0]['page'] == 1
    assert diagnostics[0]['x_pt'] == pytest.approx(x)
    assert diagnostics[0]['y_pt'] == pytest.approx(y)
    assert f'beyond {edge} edge' in diagnostics[0]['reason']
    assert diagnostics[0]['crop_box_pt'] == [0, 0, 200, 200]


def test_duplicate_marker_is_distinguished_from_missing_marker():
    pdf = marker_pdf(y=100, duplicate=True)
    reader = PdfReader(io.BytesIO(pdf))
    _, report = instrument_source(RAW)
    diagnostics = []
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        assert compiler_segment_locations(reader, document, RAW, report, KEY, tex_literal_text,
                                          diagnostics=diagnostics) is None
    assert diagnostics[0]['code'] == 'duplicate_marker'


def test_missing_markers_and_unverified_glyphs_are_distinguished():
    pdf = marker_pdf(y=100)
    _, report = instrument_source(RAW)
    for key, code in [(KEY, 'glyph_verification_failed'), ('proposal:missing', 'missing_segment_markers')]:
        reader = PdfReader(io.BytesIO(pdf))
        case = {**report, 'render_segments': [{**s, 'key': key} for s in report['render_segments']]}
        diagnostics = []
        with pdfplumber.open(io.BytesIO(pdf)) as document:
            assert compiler_segment_locations(reader, document, RAW, case, key, tex_literal_text,
                                              diagnostics=diagnostics) is None
        assert diagnostics[0]['code'] == code


def test_failed_publication_explains_source_and_aggregate_page_even_with_cached_geometry(tmp_path, monkeypatch, capsys):
    publisher = PhoneSourceBundlePublisher(tmp_path / 'docs', tmp_path / 'bundle', tmp_path / 'ledger')
    source_hash = publisher._store_object(RAW)
    publisher.source_objects[SOURCE] = {'path': SOURCE, 'sha256': source_hash, 'object': source_hash,
                                        'proposal_ids': ['caption']}
    publisher.proposals = {p['id']: {**p, 'source_sha256': source_hash}
                           for p in parse_agent_proposals(SOURCE, RAW)}
    pdf_path = tmp_path / 'document.pdf'
    pdf_path.write_bytes(marker_pdf())
    pdf_path.with_suffix('.log').write_text('Overfull \\vbox (105.41727pt too high) has occurred while \\output is active []\n')
    build = SimpleNamespace(directory=tmp_path, mapping={'source_tex': SOURCE})
    _, report = instrument_source(RAW)
    monkeypatch.setattr(publisher, '_compiler_source_reports', lambda *_: {SOURCE: (RAW, report)})
    # Empty geometry can come from the projection cache; diagnostics still use the exact PDF.
    diagnostics = publisher._diagnose_proposal_placements(build, [{'source_file': SOURCE}], {})
    assert diagnostics['caption'][0]['source_line'] == 2
    assert '105.41727pt' in diagnostics['caption'][0]['latex_layout_warnings'][0]
    publisher.publication_root.mkdir(parents=True)
    published = publisher.publication_root / 'paper.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_page(PdfReader(pdf_path).pages[0])
    writer.write(published)
    layout = publisher.docs_root / 'comments/.layout/paper.json'
    layout.parent.mkdir(parents=True)
    layout.write_text(json.dumps({'version': 1, 'page_count': 2, 'sources': [{}, {}]}))
    segments = iter([
        {'start_page': 1, 'page_count': 1, 'fingerprint': 'a' * 64},
        {'start_page': 2, 'page_count': 1, 'fingerprint': 'b' * 64,
         'inputs': [{'source_file': SOURCE}], '_proposal_diagnostics': diagnostics},
    ])
    monkeypatch.setattr(publisher, '_segment', lambda _: next(segments))
    pdf = publisher._pdf_entry({'filename': 'paper.pdf', 'project': 'docs-paper'})
    records = publisher._review_correspondence_artifacts([pdf])
    errors = strict_gate_failures(records)
    assert errors == []
    reason = records[0]["reason"]
    assert f'{SOURCE}:2' in reason
    assert 'paper.pdf page 2' in reason
    assert '19.35 pt beyond bottom edge' in reason
    assert records[0]['state'] == 'needs_attention'
    assert records[0]['locations'] == []
    output = capsys.readouterr().out
    detail = json.loads(next(line.split('\t', 2)[2] for line in output.splitlines() if line.startswith('MJPDF_DIAGNOSTIC\t')))
    assert detail['page'] == 2 and detail['component_page'] == 1
    assert detail['source_sha256'] == hashlib.sha256(RAW).hexdigest()
