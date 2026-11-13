# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import pdfplumber
from pypdf import PdfReader
import pytest

from thoughts_server import project_configuration as pc
from thoughts_server.latex_compiler import compile_source
from thoughts_server.native_renderer import _run
from thoughts_server.renderer_tools.review_instrumentation import stage_review_instrumentation
from thoughts_server.renderer_tools.review_source_staging import instrument_source
from thoughts_server.renderer_tools.shipout_placements import shipout_markers, compiler_span_regions
from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    PhoneSourceBundlePublisher, parse_review_comments,
)

CORPUS = r'''\documentclass{article}
\usepackage{amsmath,amsfonts,array,hyperref,fvextra,xspace,microtype}
\DefineVerbatimEnvironment{DocCode}{Verbatim}{breaklines=true,breakanywhere=true,frame=single}
\newcommand{\termSimulator}{\texttt{simulator}\xspace}
\newcommand{\termSimulators}{\texttt{simulators}\xspace}
\newcommand{\name}{Benchmark\xspace}
\newcommand{\StudyCard}[3]{%
\par\noindent\begin{minipage}{\linewidth}
\textbf{#1 question.}\quad #2\par
\noindent\textbf{#1 answer.}\quad #3\par
\end{minipage}
}
\begin{document}
\section{Test}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p1}{Not rendered}{OLD}{
\paragraph{Target heading.}
\CommentAnchor{point-a}{one}{}%
\CommentAnchor{point-b}{two}{}%
First target paragraph with \emph{italic text}, \emph{a slanted end} and \textbf{bold text}.
\StudyCard{C02}{What is rank?}{
Rank identifies a process.
}

\begin{enumerate}
\item First numbered item.
\item Second numbered item.
\begin{itemize}
\item A nested bullet.
\item[] An intentionally empty label.
\end{itemize}
\end{enumerate}
\begin{tabular}{ll}
\CommentAnchor{row}{note}\hline
\multicolumn{2}{c}{\CommentAnchor{cell}{span}{Target cell}}\\
\hline
A&\CommentAnchor{second-cell}{note}{}Second cell\\
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{delete-row}{note}{deleted&row\\}{}
\hline
Last&row\\
\hline
\end{tabular}
\[\CommentAnchor{math}{note}{x+y=z}\]
\[
\hat g_N=\frac{1}{N}\sum_{i=1}^{N}R_iZ_i.
\]
\[
s_0 \xrightarrow[\,r_0=0\,]{a_0=\text{``The''}} s_1.
\]
\[
\begin{aligned}
\nabla_\theta J(\theta\mid q)&=\sum_y R(q,y)\nabla_\theta\pi_\theta(y\mid q)\\
&=\mathbb{E}_{Y\sim\pi_\theta(\cdot\mid q)}[R(q,Y)\nabla_\theta\log\pi_\theta(Y\mid q)].
\end{aligned}
\]
\begin{figure}[p]\centering
\CommentAnchor{float}{note}{}Float target
\caption{Caption target comparing a deliberately long description with enough words to force the caption package to measure and then typeset it across multiple lines.}\end{figure}
\newpage
Final target.
}
\CommentAnchor{code-first}{code point}{}%
\begin{DocCode}
first_code_line()
% Code example: \end{document}
second_code_line()
\end{DocCode}
\CommentAnchor{terms}{words}{Selected\_token \termSimulator. More \termSimulators work in \name{}.}
\CommentAnchor{last}{note}{}%
\end{document}
'''


def register_project(docs, project, *sources):
    """Declare LaTeX sources in the Project's current manifest, the sole runtime authority."""
    default, folder = str(uuid.uuid4()), str(uuid.uuid4())
    pc.save(docs, {'schema_version': 5, 'kind': 'thoughts-project', 'id': project.name, 'name': project.name,
                   'default_folder_id': default, 'source_folders': [
                       {'folder_id': default, 'path': pc.default_folder(project.name), 'document_id': str(uuid.uuid4())},
                       {'folder_id': folder, 'path': project.name, 'document_id': str(uuid.uuid4())}],
                   'documents': [{'document_id': str(uuid.uuid4()), 'source': {'folder_id': folder, 'path': source},
                                  'format': 'latex', 'included': True} for source in sources]})


def glyphs(pdf):
    with pdfplumber.open(pdf) as document:
        return [[(c['text'], round(c['x0'], 4), round(c['top'], 4),
                  round(c['x1'], 4), round(c['bottom'], 4)) for c in p.chars]
                for p in document.pages]


def test_source_map_preserves_exact_unicode_bytes_and_skips_hidden_branches():
    raw = (r'\begin{document}' '\n' r'\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p}{Comment}{'
           r'\CommentAnchor{hidden}{old}{No}}{'
           r'\CommentAnchor{visible}{café}{Caffè}}' '\n' r'\end{document}').encode()
    generated, report = instrument_source(raw)
    assert report['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert generated.count(b'\n') == raw.count(b'\n')
    assert {m['key'] for m in report['markers']} == {'proposal:p', 'comment:visible'}
    assert b''.join(generated[s['generated_start']:s['generated_end']] for s in report['segments']) == raw
    for s in report['segments']:
        assert generated[s['generated_start']:s['generated_end']] == raw[s['source_start']:s['source_end']]


def test_verbatim_and_macro_definition_do_not_become_live_markers():
    raw = (r'\newcommand{\example}{\CommentAnchor{definition}{x}{Y}}' '\n'
           r'\begin{document}\begin{verbatim}' '\n'
           r'\CommentAnchor{example}{x}{Y}' '\n'
           r'\end{verbatim}\CommentAnchor{real}{x}{}Real text.\end{document}').encode()
    _, report = instrument_source(raw)
    assert {m['key'] for m in report['markers']} == {'comment:real'}


@pytest.fixture(scope='module', params=['legacy', 'explicit-highlight', 'explicit-strikethrough', 'dated-proposal'])
def compiled(tmp_path_factory, request):
    if not all(shutil.which(name) for name in ('latexmk', 'pdflatex', 'qpdf')):
        pytest.skip('real LaTeX toolchain is required for compiler placement conformance')
    docs = tmp_path_factory.mktemp('shipout')
    project = docs / 'docs-fixture'
    source = project / 'tex/source/corpus.tex'
    source.parent.mkdir(parents=True)
    corpus = CORPUS
    if request.param in {'explicit-highlight', 'explicit-strikethrough'}:
        raw = corpus.encode()
        for comment in reversed(parse_review_comments('corpus.tex', raw)):
            if comment['explicit_body'] and comment['target']['byte_end'] > comment['target']['byte_start']:
                at = comment['macro_start']
                raw = raw[:at] + raw[at:].replace(b'\\CommentAnchor', b'\\CommentStrikethrough' if request.param == 'explicit-strikethrough' else b'\\CommentHighlight', 1)
        corpus = raw.decode()
    if request.param == "dated-proposal":
        corpus = corpus.replace(r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{", r"\agentProposedEdit[created=2026-08-27T10:03:05+02:00,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{")
    source.write_text(corpus)
    original = source.read_bytes()
    register_project(docs, project, 'tex/source/corpus.tex')
    build = compile_source(docs, project, 'corpus.tex', run=_run)
    assert source.read_bytes() == original
    clean = docs / 'clean'
    clean.mkdir()
    baseline_source = clean / 'source.tex'
    baseline_source.write_text(corpus.replace('[created=2026-08-27T10:03:05+02:00]', ''))
    wrapper = clean / 'baseline.tex'
    # Independent baseline: none of the compiler instrumentation or its hooks.
    wrapper.write_text(r'\long\def\CommentAnchor#1#2{}'
                       + r'\long\def\CommentHighlight#1#2#3{{#3}}'
                       + r'\long\def\CommentStrikethrough#1#2#3{{#3}}'
                       + r'\long\def\agentProposedEdit[#1]#2#3#4#5{#5}' + '\n'
                       + r'\input{' + str(baseline_source) + '}\n')
    result = subprocess.run(['latexmk', '-pdf', '-interaction=nonstopmode', '-halt-on-error',
                             f'-output-directory={clean}', str(wrapper)], cwd=clean, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr
    actual, baseline = glyphs(build.pdf), glyphs(clean / 'baseline.pdf')
    assert len(actual) == len(baseline)
    for emitted, expected in zip(actual, baseline, strict=True):
        assert len(emitted) == len(expected)
        for char, original_char in zip(emitted, expected, strict=True):
            assert char[0] == original_char[0]
            # Opening PDF text runs for invisible graphics markers quantizes
            # origins to PDF decimal precision; 0.02pt is below 0.2px at 600dpi.
            assert char[1:] == pytest.approx(original_char[1:], abs=0.02)

    return docs, source, build


def test_real_compilation_preserves_layout_and_observes_float_shipout(compiled):
    _, _, build = compiled
    reader = PdfReader(build.pdf)
    markers = shipout_markers(reader)
    point = lambda key: next(iter(markers['comment:' + key]['point'].values()))
    assert point('point-a').point == point('point-b').point
    assert point('point-a').page == 1
    assert point('last').page == 2
    assert point('code-first').page == 2
    assert point('float').page == 3  # The source order is before page 2, shipout is after it.
    with pdfplumber.open(build.pdf) as pdf:
        for key, text in [('cell', 'Target cell'), ('math', 'x+y=z')]:
            evidence = markers['comment:' + key]
            assert compiler_span_regions(pdf, evidence['start'][1], evidence['end'][1], text)


def test_producer_refuses_synctex_only_points_and_does_not_downgrade_spans(compiled, tmp_path):
    _, source, build = compiled
    raw = source.read_bytes()
    publisher = object.__new__(PhoneSourceBundlePublisher)
    publisher.objects = tmp_path
    object_id = hashlib.sha256(raw).hexdigest()
    (tmp_path / object_id).write_bytes(raw)
    source_file = 'docs-fixture/tex/source/corpus.tex'
    comments = parse_review_comments(source_file, raw)
    comments.append({'id': 'unobserved', 'source_file': source_file, 'line': 1,
                     'target': {'byte_start': 0, 'byte_end': 0}})
    publisher.review_comments = {c['id']: c for c in comments}
    publisher.source_objects = {source_file: {'object': object_id, 'review_comment_ids': list(publisher.review_comments)}}
    publisher.logical_comments = {}
    def forbidden(*args, **kwargs):
        pytest.fail('SyncTeX cannot certify a point')
    publisher._synctex_line_boxes = forbidden
    record = SimpleNamespace(directory=build.pdf.parent, mapping={'pdf_name': build.pdf.name})
    locations = publisher._build_review_comment_locations(record, [{'source_file': source_file}])
    assert 'unobserved' not in locations
    assert locations['point-a'][0]['verification']['resolver_version'] == 3
    assert locations['cell'][0]['regions']
    # A span whose captured source no longer equals the selected glyphs must stay unresolved.
    publisher.review_comments['cell']['target']['byte_end'] -= 2
    assert 'cell' not in publisher._build_review_comment_locations(record, [{'source_file': source_file}])


def test_proposal_segments_include_generated_list_labels_and_deferred_float(compiled):
    from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations
    from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text
    _, source, build = compiled
    raw = source.read_bytes()
    _, report = instrument_source(raw)
    reader = PdfReader(build.pdf)
    with pdfplumber.open(build.pdf) as document:
        fragments = compiler_segment_locations(reader, document, raw, report, "proposal:p1", tex_literal_text)
    assert fragments
    assert {part["page"] for part in fragments} == {1, 2, 3}
    text = ''.join(part["text"] for part in fragments)
    assert '1.' in text and '2.' in text
    assert 'Float' in text
    assert 'C02question.' in text.replace(' ', '')
    assert 'C02answer.' in text.replace(' ', '')


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_real_glyphs_and_destinations_use_the_same_rotated_crop(compiled, tmp_path, rotation):
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject
    from thoughts_server.renderer_tools.shipout_placements import PageSpace
    _, _, build = compiled
    original = PdfReader(build.pdf)
    original_markers = shipout_markers(original)['comment:cell']
    writer = PdfWriter(clone_from=build.pdf)
    for page in writer.pages:
        page.cropbox = RectangleObject([40, 30, 560, 760])
        page.rotate(rotation)
    target = tmp_path / 'rotated.pdf'
    writer.write(target)
    markers = shipout_markers(PdfReader(target))['comment:cell']
    with pdfplumber.open(build.pdf) as pdf:
        base = compiler_span_regions(pdf, original_markers['start'][1], original_markers['end'][1], 'Target cell')
    with pdfplumber.open(target) as pdf:
        actual = compiler_span_regions(pdf, markers['start'][1], markers['end'][1], 'Target cell')
    assert base and actual
    width, height = map(float, (original.pages[0].mediabox.width, original.pages[0].mediabox.height))
    space = PageSpace((0, 0, width, height), (40, 30, 560, 760), rotation)
    expected = [space.normalized_rect(r[0]*width, (1-r[3])*height, r[2]*width, (1-r[1])*height)
                for r in base[0]['regions']]
    for observed, wanted in zip(actual[0]['regions'], expected, strict=True):
        assert observed == pytest.approx(wanted)
    start = original_markers['start'][1]
    assert markers['start'][1].point == pytest.approx(space.normalized(start.x, start.y))


def test_code_point_is_on_first_emitted_code_line_and_word_boxes_resolve(compiled):
    from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations
    from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text
    _, source, build = compiled
    markers = shipout_markers(PdfReader(build.pdf))
    point = next(iter(markers['comment:code-first']['point'].values()))
    with pdfplumber.open(build.pdf) as pdf:
        page = pdf.pages[point.page - 1]
        words = page.extract_words()
        target = next(word for word in words if word['text'] == 'first_code_line()')
        assert target['x0'] / page.width == pytest.approx(point.point[0], abs=0.002)
        assert target['top'] / page.height <= point.point[1] <= target['bottom'] / page.height
        _, report = instrument_source(source.read_bytes())
        fragments = compiler_segment_locations(PdfReader(build.pdf), pdf, source.read_bytes(), report, 'comment:terms', tex_literal_text)
        assert fragments and sum(part.get('method') == 'tex_shipout_box' for part in fragments) == 1


def test_actual_landscape_and_scaled_table_follow_the_pdf_graphics_transform(tmp_path):
    from pypdf import PdfWriter
    from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations
    from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text
    if not all(shutil.which(name) for name in ('latexmk', 'pdflatex', 'qpdf')):
        pytest.skip('real LaTeX toolchain required')
    project = tmp_path / 'docs-landscape'
    source = project / 'tex/source/main.tex'
    source.parent.mkdir(parents=True)
    tile = PdfWriter()
    tile.add_blank_page(width=16, height=24)
    with (project / 'tile.pdf').open('wb') as handle:
        tile.write(handle)
    source.write_text(r'''\documentclass{article}
\usepackage{graphicx,pdflscape,array,microtype}
\begin{document}
Portrait introduction.
\begin{landscape}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{landscape}{metadata}{}{
\resizebox{.7\linewidth}{!}{\begin{tabular}{ll}
\CommentAnchor{matrix}{metadata}{Complete matrix}&Second cell\\
Scaled row&\includegraphics[width=12pt]{tile.pdf}\\
\end{tabular}}
}
\end{landscape}
\end{document}
''')
    register_project(tmp_path, project, 'tex/source/main.tex')
    build = compile_source(tmp_path, project, 'main.tex', run=_run)
    raw = source.read_bytes()
    _, report = instrument_source(raw)
    with pdfplumber.open(build.pdf) as document:
        reader = PdfReader(build.pdf)
        locations = compiler_segment_locations(reader, document, raw, report, 'proposal:landscape', tex_literal_text)
        assert locations and {part['page'] for part in locations} == {2}
        assert any(part.get('method') == 'tex_shipout_box' for part in locations)
        page = document.pages[1]
        assert page.rotation == 90
        evidence = shipout_markers(reader)['comment:matrix']
        spans = compiler_span_regions(document, evidence['start'][1], evidence['end'][1], 'Complete matrix')
        assert spans
        regions = [region for part in spans for region in part['regions']]
        # Independent PDF text extraction supplies expected visible glyph boxes.
        text = ''.join(char['text'] for char in page.chars)
        index = text.index('Completematrix')
        for char in page.chars[index:index + len('Completematrix')]:
            x, y = (char['x0'] + char['x1']) / (2 * page.width), (char['top'] + char['bottom']) / (2 * page.height)
            assert any(left <= x <= right and top <= y <= bottom for left, top, right, bottom in regions)


def test_word_end_before_font_change_can_ship_on_the_following_page(tmp_path):
    from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations
    from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text
    if not all(shutil.which(name) for name in ('latexmk', 'pdflatex', 'qpdf')):
        pytest.skip('real LaTeX toolchain required')
    project = tmp_path / 'docs-page-boundary'
    source = project / 'tex/source/main.tex'
    source.parent.mkdir(parents=True)
    source.write_text(r'''\documentclass{article}
\usepackage[paperwidth=200pt,paperheight=200pt,textwidth=70pt,textheight=24pt]{geometry}
\parindent=0pt\parskip=0pt\clubpenalty=0\widowpenalty=0\rightskip=0pt plus 1fil
\begin{document}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{page-boundary}{metadata}{}{
First line.\par
Final choice. \textbf{Next word on second page.}
}
\end{document}
''')
    register_project(tmp_path, project, 'tex/source/main.tex')
    build = compile_source(tmp_path, project, 'main.tex', run=_run)
    raw = source.read_bytes()
    _, report = instrument_source(raw)
    reader = PdfReader(build.pdf)
    segment = next(value for value in report['render_segments'] if raw[value['source_start']:value['source_end']] == b'choice.')
    assert segment['end_after_font_prefix']
    evidence = shipout_markers(reader)['proposal:page-boundary']
    stem = f"span-{segment['segment']}"
    start, end = evidence[stem+'-start'][1], evidence[stem+'-end'][1]
    assert start.page < end.page
    with pdfplumber.open(build.pdf) as document:
        fragments = compiler_segment_locations(reader, document, raw, report, 'proposal:page-boundary', tex_literal_text)
        assert fragments
        choice = [part for part in fragments if part['segment'] == segment['segment']]
        assert ''.join(part['text'] for part in choice) == 'choice.'
        assert {part['page'] for part in choice} == {start.page}
        assert compiler_span_regions(document, start, end, 'wrong.', word=True, end_after_font_prefix=True) is None


def test_exact_build_snapshot_retains_compiler_log(compiled, tmp_path):
    from thoughts_server.renderer_tools.comment_anchors import cache_build, build_cache_dir
    docs, source, build = compiled
    # Work on a copy because caching stamps provenance into the PDF.
    pdf = tmp_path / 'component.pdf'
    shutil.copy2(build.pdf, pdf)
    build_id = cache_build(docs, build.source_id, source, pdf, build.synctex,
                           build.fls, builds_root=tmp_path / 'snapshots')
    snapshot = build_cache_dir(docs, build_id, tmp_path / 'snapshots')
    assert (snapshot / 'document.log').read_bytes() == build.fls.with_suffix('.log').read_bytes()
