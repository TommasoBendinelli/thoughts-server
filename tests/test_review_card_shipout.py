# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
from types import SimpleNamespace

import pdfplumber
import pytest

from thoughts_server.latex_compiler import compile_source
from thoughts_server.native_renderer import _run
from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    PhoneSourceBundlePublisher,
    parse_review_card_anchors,
)
from thoughts_server.renderer_tools.review_source_staging import instrument_source
from thoughts_server.review_reanchor import _source_anchor_projection

ALL_REDUCE = "500c4628-ad12-48f1-b45b-f2c1bc46a237"
ALPS = "d15d88de-9fc8-47fb-a47b-65048cd6f49a"
ANSWER = "An all-reduce combines values from every rank and returns the result to every rank."
ALPS_ANSWER = "Alps GH200 nodes contain four Grace-Hopper modules."


def span(identity, text):
    return (
        rf"\ReviewCardMaskStart{{{identity}}}{{0}}%" + "\n" + text + "\n"
        + rf"\ReviewCardMaskEnd{{{identity}}}{{0}}%" + "\n"
    )


def corpus(moved):
    alps = r"\paragraph{Alps facts.}" + span(ALPS, ALPS_ANSWER)
    reduce = r"\StudyCard{C05}{What does an all-reduce do?}{" + span(ALL_REDUCE, ANSWER) + "}\n"
    return (
        r"\documentclass{article}\usepackage{hyperref}" + "\n"
        + r"\providecommand{\ReviewCardMaskStart}[2]{}" + "\n"
        + r"\providecommand{\ReviewCardMaskEnd}[2]{}" + "\n"
        + r"\newcommand{\StudyCard}[3]{\par\noindent\begin{minipage}{\linewidth}"
        + r"\textbf{#1 question.}\quad #2\par\textbf{#1 answer.}\quad #3\par\end{minipage}}"
        + "\n" + r"\begin{document}" + "\n"
        # Duplicate rendered text is deliberate: a global answer-text search is insufficient.
        + ANSWER + "\n" + r"\par" + "\n"
        + (alps if moved else "")
        + r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{study-proposal}{Keep the remaining cards reviewable.}{}{"
        + (r"\newpage" if moved else "") + "\n" + reduce
        + ("" if moved else r"\newpage" + "\n" + alps)
        + "}\n" + r"\end{document}" + "\n"
    )


def publisher_for(tmp_path, raw, source_file):
    publisher = object.__new__(PhoneSourceBundlePublisher)
    publisher.objects = tmp_path / "objects"
    publisher.objects.mkdir(exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    (publisher.objects / digest).write_bytes(raw)
    publisher.source_objects = {source_file: {"object": digest}}
    publisher.review_card_anchors = {
        (anchor["card_id"], anchor["segment_id"]): anchor
        for anchor in parse_review_card_anchors(source_file, raw)
    }
    return publisher


def test_numbered_section_with_figure_and_page_break_compiles_into_complete_regions(tmp_path):
    from PIL import Image

    from thoughts_server.review_creation import mark_sources, resolve_source_segments

    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    project = tmp_path / "docs-fixture"
    source = project / "tex/source/cards.tex"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (200, 100), "blue").save(source.parent / "diagram.png")
    text = (
        "\\documentclass{article}\\usepackage{graphicx}\n"
        "\\providecommand{\\ReviewCardMaskStart}[2]{}\n"
        "\\providecommand{\\ReviewCardMaskEnd}[2]{}\n"
        "\\begin{document}\nOutside the answer.\n"
        "\\section{Selected section}\nFirst selected paragraph.\n"
        "\\begin{figure}[h]\n\\includegraphics[width=3cm]{diagram.png}\n"
        "\\caption{Selected figure caption.}\n\\end{figure}\n"
        "\\clearpage\nSecond selected paragraph.\n"
        "\\section{Other section}\nNot selected.\n\\end{document}\n"
    )
    path = "docs-fixture/tex/source/cards.tex"
    segments = resolve_source_segments({path: text}, [
        {"source_file": path, "line_start": 6, "line_end": 13},
    ])
    changed, _ = mark_sources({path: text}, [{"card_id": ALL_REDUCE, "source_segments": segments}])
    raw = changed[path]
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, project, 'tex/source/cards.tex')
    build = compile_source(tmp_path, project, "cards.tex", run=_run)
    assert source.read_bytes() == raw
    publisher = publisher_for(tmp_path, raw, path)
    record = SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name})
    fragments = publisher._build_review_card_fragments(record, [{"source_file": path, "synctex_path": path}], {})
    regions = fragments[(ALL_REDUCE, "0")]
    assert {region["page"] for region in regions} == {1, 2}
    assert any(region["verification_method"] == "tex_shipout_box" for region in regions)
    words = []
    with pdfplumber.open(build.pdf) as document:
        for region in regions:
            page = document.pages[region["page"] - 1]
            words.append(page.crop((region["left"] * page.width - .05, region["top"] * page.height - .05,
                                    region["right"] * page.width + .05,
                                    region["bottom"] * page.height + .05)).extract_text())
    selected = " ".join(words)
    assert "First selected paragraph." in selected and "Second selected paragraph." in selected
    assert "Selected figure caption." in selected
    assert "Outside the answer" not in selected and "Not selected" not in selected


@pytest.mark.parametrize("row_count", [1, 65])
def test_card_spanning_longtable_headers_has_verified_pdf_regions(tmp_path, row_count):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    source_file = "docs-fixture/tex/source/cards.tex"
    source = tmp_path / source_file
    source.parent.mkdir(parents=True)
    raw = (
        r"\documentclass{article}\usepackage{longtable,hyperref}" + "\n"
        + r"\providecommand{\ReviewCardMaskStart}[2]{}" + "\n"
        + r"\providecommand{\ReviewCardMaskEnd}[2]{}" + "\n"
        + r"\begin{document}" + "\n"
        + "Outside the answer.\n"
        + span(ALL_REDUCE,
            "Selected introduction.\n"
            + r"\begin{longtable}{|l|l|}" + "\n"
            + r"\hline\textbf{Field} & \textbf{Meaning} \\ \hline" + "\n"
            + r"\endfirsthead" + "\n"
            + r"\hline\textbf{Field} & \textbf{Meaning} \\ \hline" + "\n"
            + r"\endhead" + "\n"
            + r"\hline" + "\n"
            + r"\endfoot" + "\n"
            + r"\hline" + "\n"
            + r"\endlastfoot" + "\n"
            + "".join(f"Agent {number} & Identifies profile {number}. \\\\ \\hline\n"
                      for number in range(row_count))
            + r"\end{longtable}" + "\n"
            + "Selected conclusion.")
        + "Outside again.\n"
        + r"\end{document}" + "\n"
    ).encode()
    source.write_bytes(raw)
    _, report = instrument_source(raw)
    assert report["unresolved"] == []
    from test_shipout_placements import register_project
    register_project(tmp_path, tmp_path / "docs-fixture", 'tex/source/cards.tex')
    build = compile_source(tmp_path, tmp_path / "docs-fixture", "cards.tex", run=_run)
    publisher = publisher_for(tmp_path, raw, source_file)
    fragments = publisher._build_review_card_fragments(
        SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name}),
        [{"source_file": source_file, "synctex_path": source_file}], None,
    )[(ALL_REDUCE, "0")]
    with pdfplumber.open(build.pdf) as pdf:
        if row_count > 1:
            assert len(pdf.pages) > 1
            assert len({fragment["page"] for fragment in fragments}) > 1
        selected = " ".join(
            pdf.pages[fragment["page"] - 1].crop((
                fragment["left"] * pdf.pages[fragment["page"] - 1].width - .05,
                fragment["top"] * pdf.pages[fragment["page"] - 1].height - .05,
                fragment["right"] * pdf.pages[fragment["page"] - 1].width + .05,
                fragment["bottom"] * pdf.pages[fragment["page"] - 1].height + .05,
            )).extract_text() or ""
            for fragment in fragments
        )
    assert "Selected introduction." in selected
    assert "Agent 0" in selected
    assert "Identifies profile 0." in selected
    assert f"Identifies profile {row_count - 1}." in selected
    assert "Selected conclusion." in selected
    assert "Outside the answer." not in selected
    assert "Outside again." not in selected


@pytest.mark.parametrize("moved", [False, True])
def test_real_cards_follow_their_compiled_answers_inside_proposals(tmp_path, moved, monkeypatch):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    project = tmp_path / "docs-fixture"
    source = project / "tex/source/cards.tex"
    source.parent.mkdir(parents=True)
    raw = corpus(moved).encode()
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, project, 'tex/source/cards.tex')
    build = compile_source(tmp_path, project, "cards.tex", run=_run)
    assert source.read_bytes() == raw
    source_file = "docs-fixture/tex/source/cards.tex"
    publisher = publisher_for(tmp_path, raw, source_file)
    record = SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name})
    inputs = [{"source_file": source_file, "synctex_path": source_file}]

    def forbidden(*_args, **_kwargs):
        pytest.fail("a source card must never use approximate SyncTeX or text-search fallback")

    monkeypatch.setattr(publisher, "_synctex_line_boxes", forbidden)
    monkeypatch.setattr(publisher, "_review_card_text_fragments", forbidden)
    # Deliberately provide a plausible but incorrect box: it must be ignored.
    bogus = {(ALL_REDUCE + ":0", "mask"): {1: [(70, 100, 400, 150)]}}
    fragments = publisher._build_review_card_fragments(record, inputs, bogus)
    assert set(fragments) == {(ALL_REDUCE, "0"), (ALPS, "0")}
    with pdfplumber.open(build.pdf) as document:
        for identity, expected, expected_page in (
            (ALL_REDUCE, ANSWER, 2 if moved else 1),
            (ALPS, ALPS_ANSWER, 1 if moved else 2),
        ):
            regions = fragments[(identity, "0")]
            assert {region["page"] for region in regions} == {expected_page}
            words = []
            for region in regions:
                page = document.pages[region["page"] - 1]
                box = (region["left"] * page.width - .05, region["top"] * page.height - .05,
                       region["right"] * page.width + .05, region["bottom"] * page.height + .05)
                words.append(page.crop(box).extract_text())
                assert region["resolver_version"] == 3
            assert " ".join(words) == expected
            anchor = copy.deepcopy(publisher.review_card_anchors[(identity, "0")])
            anchor["placement"] = {"status": "anchored", "regions": [
                {**region, "pdf_filename": "concepts.pdf"} for region in regions
            ]}
            assert _source_anchor_projection("concepts.pdf", [anchor])[0] == expected_page

    # A PDF without compiler observations cannot inherit the old plausible rectangle.
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "baseline.tex").write_text(r"\long\def\agentProposedEdit[#1]#2#3#4#5{#5}" + "\n" + raw.decode())
    process = subprocess.run(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
                              "baseline.tex"], cwd=clean, capture_output=True, text=True)
    assert process.returncode == 0, process.stdout[-3000:]
    plain = SimpleNamespace(directory=clean, mapping={"pdf_name": "baseline.pdf"})
    assert publisher._build_review_card_fragments(plain, inputs, bogus) == {}
    from test_shipout_placements import glyphs
    actual, baseline = glyphs(build.pdf), glyphs(clean / "baseline.pdf")
    assert len(actual) == len(baseline)
    for page, original_page in zip(actual, baseline, strict=True):
        assert len(page) == len(original_page)
        for char, original in zip(page, original_page, strict=True):
            assert char[0] == original[0]
            assert char[1:] == pytest.approx(original[1:], abs=.02)


def test_card_inside_local_macro_without_proposal_is_instrumented():
    raw = corpus(True).replace(
        r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{study-proposal}{Keep the remaining cards reviewable.}{}{", "{"
    ).encode()
    _, report = instrument_source(raw)
    assert {m["key"] for m in report["markers"]} == {"card:" + x + ":0" for x in (ALL_REDUCE, ALPS)}
    assert not report["unresolved"]


@pytest.mark.parametrize("body", [
    span(ALL_REDUCE, "one") + span(ALL_REDUCE, "two"),
    rf"\ReviewCardMaskStart{{{ALL_REDUCE}}}{{0}}No end.",
    r"\begin{verbatim}" + "\n" + span(ALL_REDUCE, "example") + r"\end{verbatim}",
    r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p}{note}{" + span(ALL_REDUCE, "discarded") + "}{replacement}",
])
def test_invalid_or_nonrendered_card_pairs_have_no_observed_target(body):
    _, report = instrument_source((r"\begin{document}" + body + r"\end{document}").encode())
    assert not any(m["key"].startswith("card:") for m in report["markers"])
    assert not parse_review_card_anchors("docs-fixture/tex/source/cards.tex", body.encode())


def test_card_revised_in_proposal_uses_only_replacement_markers():
    raw = ("Préface.\n" + r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{revise-card}{Keep its identity.}{"
           + span(ALL_REDUCE, "Previous answer.") + "}{"
           + span(ALL_REDUCE, ANSWER) + "}").encode()
    anchors = parse_review_card_anchors("docs-fixture/tex/source/cards.tex", raw)
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor["card_id"] == ALL_REDUCE
    assert ANSWER.encode() in raw[anchor["byte_start"]:anchor["byte_end"]]
    assert b"Previous answer" not in raw[anchor["byte_start"]:anchor["byte_end"]]
    _, report = instrument_source(raw)
    assert {m["key"] for m in report["markers"] if m["key"].startswith("card:")} == {
        "card:" + ALL_REDUCE + ":0"
    }


def test_projection_rejects_legacy_geometry_even_if_marked_anchored():
    anchor = {"card_id": ALL_REDUCE, "segment_id": "0", "placement": {
        "status": "anchored", "regions": [{"pdf_filename": "concepts.pdf", "page": 1,
                                            "left": .1, "top": .2, "right": .8, "bottom": .3}],
    }}
    assert _source_anchor_projection("concepts.pdf", [anchor]) is None


def test_compiler_card_covers_numbered_wrapped_code_without_changing_layout(tmp_path):
    if not shutil.which("latexmk"):
        pytest.skip("real LaTeX toolchain required")
    source_file = "docs-code/tex/source/code.tex"
    source = tmp_path / source_file
    source.parent.mkdir(parents=True)
    code = ("alpha_call(one)\n\n# literal code comment\n"
            + "result = " + " + ".join("value" + str(i) for i in range(35)) + "\n")
    raw = (r"\documentclass{article}\usepackage{fvextra,hyperref}" + "\n"
           + r"\DefineVerbatimEnvironment{DocCode}{Verbatim}{breaklines=true,numbers=left,firstnumber=20}"
           + "\n" + r"\providecommand{\ReviewCardMaskStart}[2]{}"
           + r"\providecommand{\ReviewCardMaskEnd}[2]{}" + "\n"
           + r"\begin{document}" + "\n"
           + span(ALL_REDUCE, r"\begin{DocCode}" + "\n" + code + r"\end{DocCode}")
           + r"\end{document}" + "\n").encode()
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, tmp_path / "docs-code", 'tex/source/code.tex')
    build = compile_source(tmp_path, tmp_path / "docs-code", "code.tex", run=_run)
    publisher = publisher_for(tmp_path, raw, source_file)
    fragments = publisher._build_review_card_fragments(
        SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name}),
        [{"source_file": source_file, "synctex_path": source_file}], None,
    )
    assert fragments.get((ALL_REDUCE, "0"))
    with pdfplumber.open(build.pdf) as pdf:
        observed = []
        for region in fragments[(ALL_REDUCE, "0")]:
            page = pdf.pages[region["page"] - 1]
            observed.append(page.crop((region["left"] * page.width - .02,
                                       region["top"] * page.height - .02,
                                       region["right"] * page.width + .02,
                                       region["bottom"] * page.height + .02)).extract_text())
    assert "".join("".join(observed).split()) == "".join(code.split())
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "baseline.tex").write_bytes(raw)
    process = subprocess.run(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
                              "baseline.tex"], cwd=clean, capture_output=True, text=True)
    assert process.returncode == 0, process.stdout[-3000:]
    from test_shipout_placements import glyphs
    actual, baseline = glyphs(build.pdf), glyphs(clean / "baseline.pdf")
    assert len(actual) == len(baseline)
    for page, original_page in zip(actual, baseline, strict=True):
        assert len(page) == len(original_page)
        for char, original in zip(page, original_page, strict=True):
            assert char[0] == original[0]
            assert char[1:] == pytest.approx(original[1:], abs=.02)


def test_table_formatting_arguments_are_never_instrumented_as_answer_text():
    raw = (r'\begin{document}' + span(ALL_REDUCE,
           r'\begin{tabular}{cc}\cmidrule(lr){1-2}'
           r'\makebox [0pt][c]{Accuracy}\hspace *{53.8pt} & '
           r'\boldmath \(0.9\)\enspace Model\textsuperscript{*}\\'
           r'\specialrule{.4pt}{0pt}{6pt}\end{tabular}') + r'\end{document}').encode()
    generated, report = instrument_source(raw)
    assert report['unresolved'] == []
    texts = [raw[s['source_start']:s['source_end']].decode() for s in report['render_segments']]
    assert texts == ['Accuracy', '0.9', 'Model', '*']
    assert br'\hspace *{53.8pt}' in generated
    assert br'\cmidrule(lr){1-2}' in generated


def test_empty_code_answer_is_unresolved_without_breaking_compilation():
    raw = (r'\begin{document}' + span(ALL_REDUCE, '\\begin{DocCode}\n\n\\end{DocCode}')
           + r'\end{document}').encode()
    _, report = instrument_source(raw)
    assert report['unresolved'] == ['card:' + ALL_REDUCE + ':0']
    assert report['render_segments'] == []


@pytest.mark.parametrize("separate_page", [False, True])
def test_contents_heading_repetitions_keep_separate_verified_masks(tmp_path, separate_page):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    source_file = "docs-fixture/tex/source/cards.tex"
    source = tmp_path / source_file
    source.parent.mkdir(parents=True)
    raw = (r"\documentclass{article}\usepackage{hyperref}" + "\n"
           + r"\begin{document}\tableofcontents" + "\n"
           + (r"\clearpage" if separate_page else r"\par") + "\n"
           + "Unrelated text outside the answer.\n"
           + span(ALL_REDUCE, r"\section{Collective operations}" + "\n" + ANSWER)
           + r"\end{document}" + "\n").encode()
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, tmp_path / "docs-fixture", 'tex/source/cards.tex')
    build = compile_source(tmp_path, tmp_path / "docs-fixture", "cards.tex", run=_run)
    publisher = publisher_for(tmp_path, raw, source_file)
    fragments = publisher._build_review_card_fragments(
        SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name}),
        [{"source_file": source_file, "synctex_path": source_file}], None,
    )[(ALL_REDUCE, "0")]
    assert {r["occurrence"] for r in fragments} == {1, 2}
    assert {r["page"] for r in fragments} == ({1, 2} if separate_page else {1})
    words = []
    with pdfplumber.open(build.pdf) as pdf:
        for region in fragments:
            page = pdf.pages[region["page"] - 1]
            words.append(page.crop((region["left"] * page.width - .05,
                                    region["top"] * page.height - .05,
                                    region["right"] * page.width + .05,
                                    region["bottom"] * page.height + .05)).extract_text())
    assert words.count("Collective") == 2
    assert "Unrelated" not in " ".join(words)
    anchor = copy.deepcopy(publisher.review_card_anchors[(ALL_REDUCE, "0")])
    anchor["placement"] = {"status": "anchored", "regions": [
        {**region, "pdf_filename": "cards.pdf"} for region in fragments
    ]}
    page, _, regions = _source_anchor_projection("cards.pdf", [anchor])
    assert page == (2 if separate_page else 1)
    # The per-word boxes merge into fewer masks that still cover both copies
    # of the heading and nothing outside the answer.
    assert 2 <= len(regions) < len(fragments)
    with pdfplumber.open(build.pdf) as pdf:
        masked = []
        for region in regions:
            pdf_page = pdf.pages[region["page"] - 1]
            left, top, right, bottom = region["rect"]
            masked.append(pdf_page.crop((left * pdf_page.width - .05, top * pdf_page.height - .05,
                                         right * pdf_page.width + .05, bottom * pdf_page.height + .05)).extract_text())
    assert " ".join(masked).count("Collective") == 2
    assert "Unrelated" not in " ".join(masked)
    anchor["placement"]["regions"][-1]["marker_ids"] = []
    assert _source_anchor_projection("cards.pdf", [anchor]) is None


LITERAL_LATEX = [
    r"A \texttt{\textbackslash documentclass} declaration.",
    r"A \texttt{\textbackslash begin\{document\}} line.",
    r"A \textbackslash{} alone.",
    r"A \code{\begin{document}} environment.",
    r"A \verb|\begin{document}| line and \verb*+x y+.",
]


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("body", LITERAL_LATEX)
def test_card_showing_latex_literally_is_instrumented(body, wrapped):
    # Documentation spells commands with \textbackslash, \code or \verb; a
    # literal \begin{document} there is not the fragment's document start.
    text = "Before.\n" + span(ALL_REDUCE, body)
    if wrapped:
        text = "\\documentclass{article}\n\\begin{document}\n" + text + "\\end{document}\n"
    _, report = instrument_source(text.encode())
    assert "card:" + ALL_REDUCE + ":0" in {m["key"] for m in report["markers"]}
    assert not report["unresolved"]


def test_card_with_unknown_command_stays_unresolved():
    _, report = instrument_source(("Before.\n" + span(ALL_REDUCE, r"A \mysteriousmacro{x} line.")).encode())
    assert report["unresolved"] == ["card:" + ALL_REDUCE + ":0"]
    assert any(item["reason"] == "opaque command mysteriousmacro" for item in report["uncovered"])


PICTURE = (r"\begin{center}\setlength{\unitlength}{1cm}\begin{picture}(4,1.5)" "\n"
           r"  \put(0,0){\framebox(1.5,1){\shortstack{draft\\ block}}}\put(1.5,0.5){\vector(1,0){1}}" "\n"
           r"  \multiput(2.5,0)(0.5,0){2}{\framebox(0.5,1){}}\put(3,1.2){\circle{0.2}}\thicklines\put(0,1.4){\line(1,0){4}}" "\n"
           r"\end{picture}\end{center}")


def test_card_with_a_picture_drawing_is_instrumented_as_one_box():
    # A LaTeX picture (\put, \vector, \framebox, \multiput, \circle) is placed
    # as one box, like a tikzpicture: its commands are not answer text.
    text = ("\\documentclass{article}\n\\begin{document}\nBefore.\n"
            + span(ALL_REDUCE, "The cycle:\n" + PICTURE + "\nAfter the drawing.") + "\\end{document}\n")
    generated, report = instrument_source(text.encode())
    assert "card:" + ALL_REDUCE + ":0" in {m["key"] for m in report["markers"]}
    assert not report["unresolved"]
    assert r"\thoughtsShipoutBoxBegin" in generated.decode()


def test_card_with_a_picture_drawing_compiles_into_complete_regions(tmp_path):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    project = tmp_path / "docs-fixture"
    source = project / "tex/source/cards.tex"
    source.parent.mkdir(parents=True)
    raw = (
        "\\documentclass{article}\n"
        "\\providecommand{\\ReviewCardMaskStart}[2]{}\n"
        "\\providecommand{\\ReviewCardMaskEnd}[2]{}\n"
        "\\begin{document}\nOutside the answer.\n\n"
        + span(ALL_REDUCE, "The cycle in one drawing:\n" + PICTURE + "\nAfter the drawing.") + "\nAfter the answer.\n\\end{document}\n"
    ).encode()
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, project, 'tex/source/cards.tex')
    build = compile_source(tmp_path, project, "cards.tex", run=_run)
    path = "docs-fixture/tex/source/cards.tex"
    publisher = publisher_for(tmp_path, raw, path)
    record = SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name})
    fragments = publisher._build_review_card_fragments(record, [{"source_file": path, "synctex_path": path}], {})
    regions = fragments[(ALL_REDUCE, "0")]
    with pdfplumber.open(build.pdf) as document:
        page = document.pages[0]
        selected = " ".join(page.crop((r["left"] * page.width - .05, r["top"] * page.height - .05,
                                       r["right"] * page.width + .05, r["bottom"] * page.height + .05)).extract_text()
                            for r in regions)
    assert regions and "cycle" in selected and "draft" in selected and "drawing." in selected
    assert "Outside" not in selected and "After the answer" not in selected


def test_card_showing_latex_literally_compiles_into_complete_regions(tmp_path):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    project = tmp_path / "docs-fixture"
    source = project / "tex/source/cards.tex"
    source.parent.mkdir(parents=True)
    answer = (r"Entry points have \texttt{\textbackslash documentclass} and "
              r"\texttt{\textbackslash begin\{document\}}; literally \verb|\begin{document}| too.")
    raw = (
        "\\documentclass{article}\n"
        "\\providecommand{\\ReviewCardMaskStart}[2]{}\n"
        "\\providecommand{\\ReviewCardMaskEnd}[2]{}\n"
        "\\begin{document}\nOutside the answer.\n\n"
        + span(ALL_REDUCE, answer) + "\nAfter the answer.\n\\end{document}\n"
    ).encode()
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, project, 'tex/source/cards.tex')
    build = compile_source(tmp_path, project, "cards.tex", run=_run)
    path = "docs-fixture/tex/source/cards.tex"
    publisher = publisher_for(tmp_path, raw, path)
    record = SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name})
    fragments = publisher._build_review_card_fragments(record, [{"source_file": path, "synctex_path": path}], {})
    regions = fragments[(ALL_REDUCE, "0")]
    with pdfplumber.open(build.pdf) as document:
        page = document.pages[0]
        selected = " ".join(page.crop((r["left"] * page.width - .05, r["top"] * page.height - .05,
                                       r["right"] * page.width + .05, r["bottom"] * page.height + .05)).extract_text()
                            for r in regions)
    # Adjacent region crops overlap and the backslash glyph extracts with a
    # space after it; the answer's words are covered, nothing outside it.
    compact = "".join(selected.split())
    assert regions and "\\documentclass" in compact and "literally\\begin{document}too" in compact
    assert "Outside" not in selected and "After" not in selected


def test_card_with_two_areas_and_a_generated_table_float_compiles_into_both_segments(tmp_path):
    """A server- or client-linked card: one segment per area, a table float as a whole."""
    from thoughts_server.review_source_link import expand_to_floats, line_ranges

    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    project = tmp_path / "docs-fixture"
    source = project / "tex/source/cards.tex"
    generated = project / "tex/generated/table.tex"
    source.parent.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    generated.write_text("\\begin{tabular}{ll}\nSkill & Score \\\\\nArithmetic & 8.98 \\\\\n\\end{tabular}\n")
    text = (
        "\\documentclass{article}\n"
        "\\providecommand{\\ReviewCardMaskStart}[2]{}\n"
        "\\providecommand{\\ReviewCardMaskEnd}[2]{}\n"
        "\\begin{document}\nOutside the answer.\n\n"
        "First area paragraph.\n\nNot selected between.\n\n\\clearpage\n"
        "\\begin{table}[h]\n\\centering\n\\caption{Second area table caption.}\n"
        "\\input{tex/generated/table.tex}\n\\end{table}\n"
        "Not selected after.\n\\end{document}\n"
    ).encode()
    lines = text.splitlines(keepends=True)
    first = lines.index(b"First area paragraph.\n") + 1
    caption = lines.index(b"\\caption{Second area table caption.}\n") + 1
    ranges = line_ranges(text, [first, caption], several=True)
    ranges, flags = expand_to_floats(text, ranges)
    assert flags == [False, True]
    raw = text
    for number, (a, b) in reversed(list(enumerate(ranges))):
        raw = (raw[:a] + f"\\ReviewCardMaskStart{{{ALL_REDUCE}}}{{{number}}}%\n".encode() + raw[a:b]
               + f"\\ReviewCardMaskEnd{{{ALL_REDUCE}}}{{{number}}}%\n".encode() + raw[b:])
    source.write_bytes(raw)
    path = "docs-fixture/tex/source/cards.tex"
    from test_shipout_placements import register_project
    register_project(tmp_path, project, 'tex/source/cards.tex')
    build = compile_source(tmp_path, project, "cards.tex", run=_run)
    publisher = publisher_for(tmp_path, raw, path)
    # The build's input snapshots include generated (ordering-only) files.
    generated_id = "docs-fixture/tex/generated/table.tex"
    digest = hashlib.sha256(generated.read_bytes()).hexdigest()
    (publisher.objects / digest).write_bytes(generated.read_bytes())
    publisher.source_objects[generated_id] = {"object": digest}
    record = SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name})
    fragments = publisher._build_review_card_fragments(
        record, [{"source_file": path, "synctex_path": path},
                 {"source_file": generated_id, "synctex_path": generated_id}], {})

    def covered(regions):
        with pdfplumber.open(build.pdf) as document:
            return " ".join(
                document.pages[r["page"] - 1].crop((
                    r["left"] * document.pages[r["page"] - 1].width - .05,
                    r["top"] * document.pages[r["page"] - 1].height - .05,
                    r["right"] * document.pages[r["page"] - 1].width + .05,
                    r["bottom"] * document.pages[r["page"] - 1].height + .05)).extract_text() or ""
                for r in regions)

    first_area, table_area = covered(fragments[(ALL_REDUCE, "0")]), covered(fragments[(ALL_REDUCE, "1")])
    assert {r["page"] for r in fragments[(ALL_REDUCE, "0")]} == {1}
    assert {r["page"] for r in fragments[(ALL_REDUCE, "1")]} == {2}
    assert "First area paragraph." in first_area and "Not selected" not in first_area
    assert "Second area table caption." in table_area
    assert "Arithmetic" in table_area and "8.98" in table_area
    assert "Not selected" not in table_area and "Outside" not in first_area + table_area
