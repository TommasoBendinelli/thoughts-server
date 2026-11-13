# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from types import SimpleNamespace

from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    PhoneSourceBundlePublisher,
    parse_review_card_anchors,
    proposal_fragments_cover_literal,
    rendered_text_covers_literal,
    sha256_bytes,
    tex_is_declaration_only,
    tex_literal_segments,
    tex_literal_text,
    unique_ordered_subsequence,
    usable_synctex_boxes,
)


def _rendered_document(value: str, *, copies: int = 1) -> SimpleNamespace:
    rendered = " ".join([value] * copies)
    characters = [
        {
            "x0": float(index),
            "x1": float(index + 1),
            "top": 100.0,
            "bottom": 110.0,
            "text": character,
        }
        for index, character in enumerate(rendered)
    ]
    return SimpleNamespace(
        pages=[SimpleNamespace(chars=characters, width=600.0, height=800.0)]
    )


def _review_card_publisher(tmp_path, source_file: str, raw: bytes):
    publisher = object.__new__(PhoneSourceBundlePublisher)
    publisher.objects = tmp_path / "objects"
    publisher.objects.mkdir()
    object_id = sha256_bytes(raw)
    (publisher.objects / object_id).write_bytes(raw)
    publisher.source_objects = {source_file: {"object": object_id}}
    return publisher


def test_review_card_text_fallback_handles_markers_inside_agent_proposal(tmp_path, monkeypatch):
    source_file = "docs-paper/tex/source/cards.tex"
    card_id = "025ee3d7-29c3-4c73-af95-00f992f4544b"
    answer = "Pipeline parallelism places different layer groups on different devices."
    raw = (
        r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{proposal-id}{Keep the generated notes reviewable.}{}{" "\n"
        r"\StudyCard{C10}{What is pipeline parallelism?}{" "\n"
        rf"\ReviewCardMaskStart{{{card_id}}}{{0}}%" "\n"
        f"{answer}\n"
        rf"\ReviewCardMaskEnd{{{card_id}}}{{0}}%" "\n"
        "}}\n"
    ).encode()
    anchor = parse_review_card_anchors(source_file, raw)[0]
    publisher = _review_card_publisher(tmp_path, source_file, raw)
    monkeypatch.setattr(publisher, "_unique_text_regions", lambda *_args: None)

    fragments = publisher._review_card_text_fragments(
        tmp_path / "segment.pdf",
        _rendered_document(answer),
        anchor,
    )

    assert fragments
    assert {fragment["verification_method"] for fragment in fragments} == {
        "pdf_text_ordered_unique"
    }
    assert all(0 <= float(fragment["left"]) < float(fragment["right"]) <= 1 for fragment in fragments)


def test_review_card_text_fallback_refuses_ambiguous_rendered_answer(tmp_path, monkeypatch):
    source_file = "docs-paper/tex/source/cards.tex"
    card_id = "025ee3d7-29c3-4c73-af95-00f992f4544b"
    answer = "The same answer must not be guessed."
    raw = (
        rf"\ReviewCardMaskStart{{{card_id}}}{{0}}%" "\n"
        f"{answer}\n"
        rf"\ReviewCardMaskEnd{{{card_id}}}{{0}}%" "\n"
    ).encode()
    anchor = parse_review_card_anchors(source_file, raw)[0]
    publisher = _review_card_publisher(tmp_path, source_file, raw)
    monkeypatch.setattr(publisher, "_unique_text_regions", lambda *_args: None)

    assert publisher._review_card_text_fragments(
        tmp_path / "segment.pdf",
        _rendered_document(answer, copies=2),
        anchor,
    ) == []


def test_tex_literal_text_keeps_visible_words_and_drops_mapping_metadata():
    assert tex_literal_text(r"\section{Protocol and Metric Details}") == (
        "Protocol and Metric Details"
    )
    assert tex_literal_text(r"\input{tex/fragments/appendix/protocol_metrics.tex}") == ""
    assert tex_literal_text(r"See \href{https://example.test}{the full result} \citep{paper}.") == (
        "See the full result ."
    )
    assert tex_literal_text(r"See \PdfLink{paper-id}{paper-title}{The visible title}.") == (
        "See The visible title."
    )
    assert tex_literal_text(
        r"\hypertarget{internal-id}{}\pdfbookmark[2]{Outline title}{bookmark-id}"
        r"\subsection*{Visible heading}"
    ) == "Visible heading"
    assert tex_literal_text(
        r"\begin{table}[ht]\caption{Unique floated proof caption.}\end{table}"
    ) == "Unique floated proof caption."
    assert tex_literal_text(
        r"\captionof{table}{Visible caption with \texttt{BallDrop}.}"
    ) == "Visible caption with BallDrop ."
    assert tex_literal_text(
        r"Use \code{\textbackslash IfFileExists} and \path{library/README.md}."
    ) == r"Use \textbackslash IfFileExists and library/README.md ."
    assert tex_literal_text(
        r"Before \(R_i=R(Y_i,q)\), between \[Z_i=\nabla_\theta\] after."
    ) == "Before , between after."
    assert tex_literal_text(
        r"Visible. \begin{align}x &= y \\ z &= 2\end{align} Still visible."
    ) == "Visible. Still visible."
    assert tex_literal_text(
        r"\newcommand{\HiddenFigure}[1]{Hidden caption #1.} "
        r"See \hyperlink{results-anchor}{the rendered results} in "
        r"\textcolor{black}{the final section}."
    ) == "See the rendered results in the final section ."
    assert tex_literal_text(
        r"\CommentAnchor{comment-id}{A private comment}{Visible anchored text.}"
    ) == "Visible anchored text."
    assert tex_literal_text(
        r"\begin{tabular}{P{0.24\textwidth}rr} Name & Count & Score \\ "
        r"\end{tabular}"
    ) == "Name & Count & Score"
    assert tex_literal_text(
        r"\setlength{\tabcolsep}{10pt}\bibitem{private-key} Visible citation."
    ) == "Visible citation."


def test_rendered_text_proof_requires_every_literal_token_in_source_order():
    assert rendered_text_covers_literal(
        "The source-linked PDF uses SHA-256 verification.",
        "Page heading The source-linked PDF uses generated build 41 SHA-256 verification.",
    )
    assert not rendered_text_covers_literal(
        "The source-linked PDF uses SHA-256 verification.",
        "The source-linked PDF uses verification.",
    )
    assert not rendered_text_covers_literal(
        "The source-linked PDF uses SHA-256 verification.",
        "SHA-256 verification precedes the source-linked PDF.",
    )


def test_unique_ordered_subsequence_allows_bounded_generated_text_once():
    match = unique_ordered_subsequence(
        "theliveprojectionmaterialized",
        "theliveprojectionpageheading42materialized",
    )
    assert match is not None
    indices, candidate_count = match
    assert candidate_count == 1
    assert indices[0] == 0
    assert indices[-1] == len("theliveprojectionpageheading42materialized") - 1

    interior = unique_ordered_subsequence(
        "atolmo3rlzerocodehasverifiedresults",
        "atk128olmo3rlzerocodehasverifiedresults",
    )
    assert interior is not None
    assert interior[1] == 1


def test_unique_ordered_subsequence_rejects_ambiguous_or_sparse_mapping():
    assert unique_ordered_subsequence("exactproposal", "exactproposalexactproposal") is None
    assert unique_ordered_subsequence(
        "exactproposal",
        "exact" + "x" * 300 + "proposal",
    ) is None


def test_segmented_pdf_proof_handles_large_generated_insertions_without_guessing():
    first = "First uniquely rendered source sentence."
    second = "Second independently unique source sentence."
    rendered = first + "x" * 300 + second
    characters = [
        {
            "x0": float(index),
            "x1": float(index + 1),
            "top": 100.0,
            "bottom": 110.0,
            "text": character,
        }
        for index, character in enumerate(rendered)
    ]
    document = SimpleNamespace(
        pages=[SimpleNamespace(chars=characters, height=800.0)]
    )

    assert PhoneSourceBundlePublisher._unique_ordered_text_fragments(
        document, first + " " + second
    ) is None
    segmented = PhoneSourceBundlePublisher._unique_segmented_text_fragments(
        document, [first, second]
    )

    assert segmented is not None
    assert segmented["candidate_count"] == 1
    assert {value["verification_method"] for value in segmented["fragments"]} == {
        "pdf_text_segmented_unique"
    }
    assert proposal_fragments_cover_literal(
        first + " generated table " + second,
        first + second,
        segmented["fragments"],
    )
    assert not proposal_fragments_cover_literal(
        first + " omitted words " + second,
        first + second,
        [{"verification_method": "synctex_pdf_text_alignment"}],
    )


def test_segmented_pdf_proof_binds_ambiguous_short_heading_to_adjacent_prose():
    rendered = (
        "Earlier Summary unrelated material. "
        "Summary A uniquely identifying explanation follows this heading."
    )
    characters = [
        {
            "x0": float(index),
            "x1": float(index + 1),
            "top": 100.0,
            "bottom": 110.0,
            "text": character,
        }
        for index, character in enumerate(rendered)
    ]
    document = SimpleNamespace(pages=[SimpleNamespace(chars=characters, height=800.0)])

    assert PhoneSourceBundlePublisher._unique_ordered_text_fragments(
        document,
        "Summary",
    ) is None
    assert PhoneSourceBundlePublisher._unique_segmented_text_fragments(
        document,
        ["Summary", "A uniquely identifying explanation follows this heading."],
    ) is not None


def test_segmented_pdf_proof_binds_heading_across_reordered_float_caption():
    rendered = (
        "Final diagnostics assembled from immutable counters. "
        "Final diagnostics. The verified totals are identical."
    )
    characters = [
        {
            "x0": float(index),
            "x1": float(index + 1),
            "top": 100.0,
            "bottom": 110.0,
            "text": character,
        }
        for index, character in enumerate(rendered)
    ]
    document = SimpleNamespace(pages=[SimpleNamespace(chars=characters, height=800.0)])

    assert PhoneSourceBundlePublisher._unique_segmented_text_fragments(
        document,
        [
            "Final diagnostics.",
            "Final diagnostics assembled from immutable counters.",
            "The verified totals are identical.",
        ],
    ) is not None


def test_tex_literal_segments_cover_visible_prose_around_generated_inputs():
    segments = tex_literal_segments(
        "First uniquely rendered source sentence.\n\n"
        r"\input{tex/generated/large_table.tex}"
        "\n\nSecond independently unique source sentence."
    )

    assert segments == [
        "First uniquely rendered source sentence.",
        "Second independently unique source sentence.",
    ]


def test_tex_literal_segments_separate_floated_caption_from_following_heading():
    segments = tex_literal_segments(
        r"\paragraph{Final diagnostics.}"
        "\n"
        r"\begin{table}[H]"
        "\n"
        r"\caption{A uniquely generated diagnostic table.}"
        "\n"
        r"\input{generated-table.tex}"
        "\n"
        r"\end{table}"
        "\n"
        "The verified totals are identical."
    )

    assert segments == [
        "Final diagnostics.",
        "A uniquely generated diagnostic table.",
        "The verified totals are identical.",
    ]
    assert tex_literal_segments(
        r"\newcommand{\HiddenTable}{\begin{table}Hidden caption.\end{table}}"
    ) == []


def test_after_query_includes_multiline_macro_invocation_line():
    proposal = {
        "line": 20,
        "column": 3,
        "after": {
            "line_start": 24,
            "line_end": 27,
            "column_start": 4,
            "column_end": 20,
        },
        "before": {"line_start": 23, "line_end": 23},
    }

    assert PhoneSourceBundlePublisher._proposal_query_content(proposal, "after") == {
        "line_start": 20,
        "line_end": 27,
        "column_start": 3,
        "column_end": 20,
    }
    assert PhoneSourceBundlePublisher._proposal_query_content(proposal, "before") == {
        "line_start": 23,
        "line_end": 23,
    }


def test_declaration_only_source_is_not_mistaken_for_rendered_text():
    assert tex_is_declaration_only(
        r"\newcommand{\ResultFigure}[2][0.9\linewidth]{\fbox{Visible fallback #2}}"
    )
    assert tex_is_declaration_only(
        r"\newenvironment{proofbox}[1]{\begin{quote}#1}{\end{quote}}"
    )
    assert tex_is_declaration_only(r"\bibliographystyle{plainnat}")
    assert tex_is_declaration_only(
        r"\newcommand{\firstmacro}{First hidden body.}"
        "\n"
        r"\newcommand{\secondmacro}[1]{Second hidden body #1.}"
    )
    assert not tex_is_declaration_only(r"\IfFileExists{table.tex}{\input{table.tex}}{}")
    assert not tex_is_declaration_only(r"\newcommand{\term}{Term} Visible text.")


def test_usable_synctex_boxes_clusters_points_and_normalizes_real_areas():
    assert usable_synctex_boxes(
        [
            (10.0, 20.0, 10.0, 24.0),
            (10.0, 20.0, 10.4, 24.0),
            (30.0, 40.0, 12.0, 20.0),
        ]
    ) == [
        (12.0, 20.0, 30.0, 40.0),
        (7.0, 18.0, 18.2, 26.0),
    ]


def test_proposal_dependency_resolution_is_project_bound_and_exact():
    available = {
        "docs-paper/tex/generated/table.tex": {},
        "docs-paper/tex/fragments/appendix.tex": {},
    }

    assert PhoneSourceBundlePublisher._resolve_tex_dependency(
        "docs-paper/tex/source/main.tex",
        "tex/generated/table",
        available,
    ) == "docs-paper/tex/generated/table.tex"
    assert PhoneSourceBundlePublisher._resolve_tex_dependency(
        "docs-paper/tex/source/main.tex",
        "../fragments/appendix",
        available,
    ) == "docs-paper/tex/fragments/appendix.tex"


def test_unique_text_match_reuses_open_document_without_reopening_or_closing(tmp_path, monkeypatch):
    import pdfplumber
    document = _rendered_document("A distinct rendered answer.")
    def unexpected_open(*args, **kwargs):
        raise AssertionError("An already-open immutable PDF must not be reopened per card")
    monkeypatch.setattr(pdfplumber, "open", unexpected_open)
    result = PhoneSourceBundlePublisher._unique_text_regions(
        tmp_path / "unused.pdf", "A distinct rendered answer.", document,
    )
    assert result is not None
    assert result["candidate_count"] == 1
    assert result["page"] == 1


def test_reused_document_still_rejects_matches_on_multiple_pages(tmp_path):
    document = _rendered_document("Repeated answer.")
    document.pages += _rendered_document("Repeated answer.").pages
    assert PhoneSourceBundlePublisher._unique_text_regions(
        tmp_path / "unused.pdf", "Repeated answer.", document,
    ) is None
