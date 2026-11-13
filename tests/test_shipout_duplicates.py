# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Occurrences that LaTeX reproduces elsewhere are observed but marked as duplicates."""
from __future__ import annotations

import shutil
import subprocess

import pdfplumber
from pypdf import PdfReader
import pytest

from test_compact_proposal_geometry import fragment, normalize
from test_shipout_placements import glyphs, register_project
from thoughts_server.latex_compiler import compile_source
from thoughts_server.native_renderer import _run
from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text
from thoughts_server.renderer_tools.review_source_staging import instrument_source
from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations
from thoughts_server.review_correspondence import ReviewPdfLocation

CORPUS = r'''\documentclass{article}
\usepackage{hyperref}
\begin{document}
\tableofcontents
\listoffigures
\newpage
\section{Intro}
Body text.
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{heading}{Rename}{\subsection{Old title}}{\subsection{New \emph{title}}
New body paragraph.}
\begin{figure}[h]\centering X\caption{\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{caption}{Reword}{Old}{Proposed caption}}\end{figure}
\section{Other}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{inline}{Reword}{Old}{Inline \emph{proposed} words.}
\end{document}
'''


@pytest.fixture(scope='module')
def compiled(tmp_path_factory):
    if not all(shutil.which(name) for name in ('latexmk', 'pdflatex', 'qpdf')):
        pytest.skip('real LaTeX toolchain is required for compiler placement conformance')
    docs = tmp_path_factory.mktemp('duplicates')
    project = docs / 'docs-fixture'
    source = project / 'tex/source/corpus.tex'
    source.parent.mkdir(parents=True)
    source.write_text(CORPUS)
    register_project(docs, project, 'tex/source/corpus.tex')
    build = compile_source(docs, project, 'corpus.tex', run=_run)
    clean = docs / 'clean'
    clean.mkdir()
    (clean / 'source.tex').write_text(CORPUS)
    wrapper = clean / 'baseline.tex'
    wrapper.write_text(r'\long\def\agentProposedEdit[#1]#2#3#4#5{#5}' + '\n'
                       + r'\input{' + str(clean / 'source.tex') + '}\n')
    result = subprocess.run(['latexmk', '-pdf', '-interaction=nonstopmode', '-halt-on-error',
                             f'-output-directory={clean}', str(wrapper)], cwd=clean, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr
    return source, build, clean / 'baseline.pdf'


def locations(source, build, proposal_id):
    raw = source.read_bytes()
    _, report = instrument_source(raw)
    with pdfplumber.open(build.pdf) as document:
        return compiler_segment_locations(
            PdfReader(build.pdf), document, raw, report, 'proposal:' + proposal_id, tex_literal_text,
        )


def test_copy_markers_preserve_the_contents_and_body_layout(compiled):
    _, build, baseline = compiled
    actual, expected = glyphs(build.pdf), glyphs(baseline)
    assert len(actual) == len(expected)
    for emitted, original in zip(actual, expected, strict=True):
        assert [char[0] for char in emitted] == [char[0] for char in original]
        for char, original_char in zip(emitted, original, strict=True):
            assert char[1:] == pytest.approx(original_char[1:], abs=0.02)


@pytest.mark.parametrize('proposal_id, text', [('heading', 'Newtitle'), ('caption', 'Proposedcaption')])
def test_contents_and_list_entries_are_duplicates_of_the_body_occurrence(compiled, proposal_id, text):
    source, build, _ = compiled
    found = locations(source, build, proposal_id)
    copies = [part for part in found if part['copy']]
    primaries = [part for part in found if not part['copy']]
    assert {part['page'] for part in copies} == {1}
    assert {part['page'] for part in primaries} == {2}
    assert ''.join(part['text'] for part in copies).replace(' ', '') == text
    # Every reproduced segment also has its own occurrence in the body.
    assert {part['segment'] for part in copies} <= {part['segment'] for part in primaries}


def test_text_typeset_once_has_no_duplicates(compiled):
    source, build, _ = compiled
    found = locations(source, build, 'inline')
    assert found and not any(part['copy'] for part in found)
    assert {part['page'] for part in found} == {2}


def test_duplicate_flag_survives_grouping_and_never_merges_with_the_primary():
    grouped = normalize([fragment(duplicate=True), fragment(occurrence=2, page=2)])
    assert [(location['page'], location['duplicate']) for location in grouped] == [(1, True), (2, False)]
    assert normalize([fragment()])[0]['duplicate'] is False


def test_location_model_defaults_duplicate_to_false():
    location = {
        'pdf_filename': 'paper.pdf', 'pdf_sha256': 'b' * 64, 'source_bundle_sha256': 'c' * 64,
        'build_sha256': 'a' * 64, 'page_index': 0, 'page': 1, 'role': 'after',
        'placement_kind': 'exact_text', 'regions': [[0.1, 0.2, 0.3, 0.4]],
        'proof': {'method': 'pdf_text_unique', 'verified': True, 'pdf_sha256': 'b' * 64,
                  'source_bundle_sha256': 'c' * 64, 'build_sha256': 'a' * 64},
    }
    assert ReviewPdfLocation.model_validate(location).duplicate is False
    assert ReviewPdfLocation.model_validate({**location, 'duplicate': True}).model_dump()['duplicate'] is True
