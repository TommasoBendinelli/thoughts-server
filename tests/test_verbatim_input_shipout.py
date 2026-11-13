# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import shutil
import subprocess
from types import SimpleNamespace

import pdfplumber
from pypdf import PdfReader
import pytest

from thoughts_server.latex_compiler import compile_source
from thoughts_server.native_renderer import _run
from thoughts_server.renderer_tools.review_source_staging import instrument_source
from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations
from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text

SOURCE = 'docs-verbatim/tex/source/main.tex'
ASSET = 'docs-verbatim/examples/code.txt'


def proposal(options=''):
    return (r'\begin{document}\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{external}{reason}{}{'
            + r'\VerbatimInput[' + options + r']{examples/code.txt}'
            + r'}\end{document}').encode()


def test_external_lines_preserve_source_identity_and_literal_bytes():
    raw = proposal('firstline=2,lastline=4,numbers=left,firstnumber=30')
    asset = 'ignored\r\nécho {literal} # \\command\r\n\r\nlast_line()\r\nignored'.encode()
    generated, report = instrument_source(raw, source_id=SOURCE, sources={ASSET: asset})
    assert not report['unresolved']
    lines = report['render_segments']
    assert [s['source_line'] for s in lines] == [2, 4]
    assert [s['code_line'] for s in lines] == [1, 3]
    for s in lines:
        assert s['source_file'] == ASSET
        assert s['source_sha256'] == hashlib.sha256(asset).hexdigest()
        assert asset[s['source_start']:s['source_end']].decode() == s['source_text']
    assert b'\\VerbatimInput[firstline=2,lastline=4,numbers=left,firstnumber=30]{examples/code.txt}' in generated
    assert b'\\thoughtsShipoutCodeLines' in generated


@pytest.mark.parametrize('sources,options', [({}, ''), ({ASSET: b'\xff'}, ''),
    ({ASSET: b'\n  \n'}, ''), ({ASSET: b'hello'}, 'firstline=missing'),
    ({ASSET: b'hello'}, 'firstline=9')])
def test_unavailable_or_uncertifiable_input_fails_closed(sources, options):
    _, report = instrument_source(proposal(options), source_id=SOURCE, sources=sources)
    assert report['unresolved'] == ['proposal:external']


@pytest.mark.parametrize('options', ['breaklines=true,breakanywhere=true,numbers=left,firstnumber=20',
                                    'breaklines=true,firstline=2,lastline=50'])
def test_compiled_external_lines_wrap_and_cross_pages_without_layout_changes(tmp_path, options):
    if not all(shutil.which(n) for n in ('latexmk', 'pdflatex', 'qpdf')):
        pytest.skip('real LaTeX toolchain required')
    project = tmp_path / 'docs-verbatim'
    source = tmp_path / SOURCE
    source.parent.mkdir(parents=True)
    asset = tmp_path / ASSET
    asset.parent.mkdir(parents=True)
    code = '\n'.join('line_%02d = ' % i + ' + '.join('value_%d' % j for j in range(17))
                     if i % 4 else '# literal {braces} \\command -- _%d' % i for i in range(65)) + '\n'
    asset.write_text(code)
    raw = (r'\documentclass{article}\usepackage{fvextra,hyperref}'
           + r'\usepackage[paperheight=15cm,paperwidth=14cm,margin=1.5cm]{geometry}'
           + '\n').encode() + proposal(options)
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, project, 'tex/source/main.tex')
    build = compile_source(tmp_path, project, 'main.tex', run=_run)
    from thoughts_server.renderer_tools.comment_anchors import build_source_snapshots
    from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher, BundleError
    snapshot_dir = tmp_path / 'snapshots'
    snapshot_dir.mkdir()
    snapshots = build_source_snapshots(tmp_path, SOURCE, source, project / 'build/main.fls', snapshot_dir)
    imported = next(s for s in snapshots if s['source_id'] == ASSET)
    assert imported['classification'] == 'verbatim'
    assert not imported['marker_eligible']
    publisher = object.__new__(PhoneSourceBundlePublisher)
    publisher.source_objects = {}
    cached = SimpleNamespace(directory=snapshot_dir, mapping={'source_snapshots': snapshots})
    # Publication must use the compiled version, even if the source changes later.
    asset.write_text('newer working-tree text')
    reports = publisher._compiler_source_reports(cached, [])
    raw, report = reports[SOURCE]
    asset.write_text(code)
    (snapshot_dir / imported['snapshot']).write_text('corrupted snapshot')
    with pytest.raises(BundleError, match='hash does not match'):
        publisher._compiler_source_reports(cached, [])
    diagnostics = []
    with pdfplumber.open(build.pdf) as pdf:
        fragments = compiler_segment_locations(PdfReader(build.pdf), pdf, raw, report,
                                               'proposal:external', tex_literal_text, diagnostics=diagnostics)
        assert fragments, diagnostics
        assert len({f['page'] for f in fragments}) >= 3
        assert any(len(f['regions']) > 1 for f in fragments)
        observed = ''.join(f['text'] for f in fragments)
        expected = ''.join(s['source_text'] for s in report['render_segments'])
        assert ''.join(observed.split()) == ''.join(expected.split())
        # A punctuation-only corruption must not be accepted as equivalent code.
        report['render_segments'][0]['source_text'] += '!'
        failures = []
        assert compiler_segment_locations(PdfReader(build.pdf), pdf, raw, report,
            'proposal:external', tex_literal_text, diagnostics=failures) is None
        assert failures[0]['code'] == 'glyph_verification_failed'
    assert source.read_bytes() == raw
    assert asset.read_text() == code
    # Compile without any review markers and compare every glyph's geometry.
    clean = tmp_path / 'baseline'
    clean.mkdir()
    shutil.copytree(project / 'examples', clean / 'examples')
    (clean / 'main.tex').write_bytes(br'\long\def\agentProposedEdit[#1]#2#3#4#5{#5}' + raw)
    result = subprocess.run(['latexmk', '-pdf', '-interaction=nonstopmode', '-halt-on-error', 'main.tex'],
                            cwd=clean, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-3000:]
    from test_shipout_placements import glyphs
    actual, baseline = glyphs(build.pdf), glyphs(clean / 'main.pdf')
    assert len(actual) == len(baseline)
    for page, original in zip(actual, baseline, strict=True):
        assert len(page) == len(original)
        for char, before in zip(page, original, strict=True):
            assert char[0] == before[0]
            assert char[1:] == pytest.approx(before[1:], abs=.02)


def test_repeated_includes_have_distinct_segments_and_preview_uses_original():
    raw = (r'\begin{document}\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{external}{why}{Old text.}{'
           r'\VerbatimInput{examples/code.txt}\VerbatimInput{examples/code.txt}}\end{document}').encode()
    sources = {ASSET: b'one\ntwo\n'}
    _, report = instrument_source(raw, source_id=SOURCE, sources=sources)
    assert not report['unresolved']
    assert len(report['render_segments']) == 4
    assert len({s['segment'] for s in report['render_segments']}) == 4
    generated, report = instrument_source(raw, source_id=SOURCE, sources=sources, preview_proposal_id='external')
    assert b'\\thoughtsShipoutCodeLines' not in generated
    assert all('source_file' not in s for s in report['render_segments'])


def test_marker_hook_size_does_not_grow_with_imported_line_count():
    small, _ = instrument_source(proposal(), source_id=SOURCE, sources={ASSET: b'x\n'})
    large, report = instrument_source(proposal(), source_id=SOURCE, sources={ASSET: b'x\n' * 10000})
    assert small == large
    assert len(report['render_segments']) == 10000
    assert not report['unresolved']


def test_verbatim_inside_repeated_tex_include_keeps_asset_provenance():
    raw = (r'\begin{document}\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{external}{why}{}{'
           r'\input{child}\input{child}}\end{document}').encode()
    child = ('Préface.\n' + r'\VerbatimInput{examples/code.txt}').encode()
    asset = 'écho(x)\n'.encode()
    sources = {ASSET: asset, 'docs-verbatim/tex/source/child.tex': child}
    _, report = instrument_source(raw, source_id=SOURCE, sources=sources)
    lines = [s for s in report['render_segments'] if 'code_line' in s]
    assert not report['unresolved']
    assert len(lines) == 2
    assert lines[0]['segment'] != lines[1]['segment']
    assert all(s['source_file'] == ASSET and s['source_text'] == 'écho(x)' for s in lines)
