# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import random

import pytest

from text_pdfs import text_pdf
from thoughts_server.renderer_tools import page_extraction
from thoughts_server.renderer_tools.basic_review import _NodeIndex, _ranked_nearest
from thoughts_server.renderer_tools.page_extraction import PageExtractor, page_identities

PAGES = [[f"Page {page} line {line} of a study text." for line in range(12)] for page in range(1, 6)]


def write(tmp_path, name, value):
    path = tmp_path / name
    path.write_bytes(value)
    return path


def uncached(path, kind):
    """The extraction without memo, slots or workers."""
    extractor = PageExtractor(jobs=1)
    count = len(page_identities(path))
    computed = extractor._compute(path, kind, list(range(1, count + 1)), parallel=False)
    return {number: value for number, (value, _companion) in computed.items()}


def test_identity_ignores_object_numbers_trailer_id_and_page_stamps(tmp_path):
    base = page_identities(write(tmp_path, "a.pdf", text_pdf(PAGES)))
    assert len(set(base)) == len(PAGES)
    same = [
        text_pdf(PAGES, trailer_id="f" * 32),
        text_pdf(PAGES, font_first=False),
        text_pdf(PAGES, page_extra="/ReviewSourceBuildID (b6bf7ec7) "),
    ]
    for index, value in enumerate(same):
        assert page_identities(write(tmp_path, f"same{index}.pdf", value)) == base
    edited = [list(lines) for lines in PAGES]
    edited[2][5] = "An edited line."
    changed = page_identities(write(tmp_path, "edited.pdf", text_pdf(edited)))
    assert [left == right for left, right in zip(base, changed)] == [True, True, False, True, True]


@pytest.mark.parametrize("kind", ["chars", "words", "shipout", "glyphs"])
def test_cached_and_parallel_extractions_equal_the_uncached_computation(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    path = write(tmp_path, "a.pdf", text_pdf(PAGES, marker="thoughts-shipout:comment:c1:point:1"))
    reference = uncached(path, kind)
    assert reference[1]  # real content, not an empty extraction
    numbers = range(1, len(PAGES) + 1)

    parallel = PageExtractor(jobs=3)
    assert parallel.pages(path, kind, numbers, slot="doc") == reference
    assert parallel.statistics["computed"] == len(PAGES)
    parallel.flush()

    # A new process (the next build) reads every page from the slot.
    stored = PageExtractor(jobs=1)
    assert stored.pages(path, kind, numbers, slot="doc") == reference
    assert stored.statistics == {"computed": 0, "memo": 0, "stored": len(PAGES)}
    # The same page content elsewhere hits the process memo.
    copy = write(tmp_path, "copy.pdf", text_pdf(PAGES, trailer_id="a" * 32, marker="thoughts-shipout:comment:c1:point:1"))
    assert stored.pages(copy, kind, numbers) == reference
    assert stored.statistics["computed"] == 0


def test_one_edited_page_is_the_only_page_extracted_again(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    path = write(tmp_path, "a.pdf", text_pdf(PAGES))
    first = PageExtractor(jobs=1)
    first.pages(path, "chars", range(1, 6), slot="doc")
    first.flush()
    edited = [list(lines) for lines in PAGES]
    edited[3][0] = "Changed text on page four."
    path.write_bytes(text_pdf(edited))
    second = PageExtractor(jobs=1)
    result = second.pages(path, "chars", range(1, 6), slot="doc")
    assert second.statistics == {"computed": 1, "memo": 0, "stored": 4}
    assert result == uncached(path, "chars")
    assert "".join(char[4] for char in result[4][2]).startswith("Changed text on page four")
    second.flush()
    # The slot keeps exactly the current document's pages.
    third = PageExtractor(jobs=1)
    third.pages(path, "chars", [1], slot="doc")
    slot = third._slots[("chars", "doc")]
    assert set(slot.entries) == set(page_identities(path))


def test_corrupt_stale_or_foreign_slots_fall_back_to_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    path = write(tmp_path, "a.pdf", text_pdf(PAGES))
    reference = uncached(path, "words")
    extractor = PageExtractor(jobs=1)
    extractor.pages(path, "words", range(1, 6), slot="doc")
    extractor.flush()
    slot_file = next((tmp_path / "cache/pdf-pages/words").iterdir())
    raw = slot_file.read_bytes()

    slot_file.write_bytes(raw[:-7] + b"garbage")
    corrupt = PageExtractor(jobs=1)
    assert corrupt.pages(path, "words", range(1, 6), slot="doc") == reference
    assert corrupt.statistics["computed"] == 5

    slot_file.write_bytes(raw)
    monkeypatch.setattr(page_extraction, "_IMPLEMENTATION", "f" * 64)
    upgraded = PageExtractor(jobs=1)
    assert upgraded.pages(path, "words", range(1, 6), slot="doc") == reference
    assert upgraded.statistics["computed"] == 5


def test_slots_stay_within_the_cache_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    path = write(tmp_path, "a.pdf", text_pdf(PAGES))
    extractor = PageExtractor(jobs=1)
    extractor.pages(path, "chars", range(1, 6), slot="first")
    extractor.flush()
    size = next((tmp_path / "cache/pdf-pages/chars").iterdir()).stat().st_size
    monkeypatch.setattr(page_extraction, "CACHE_BUDGET_BYTES", size * 2)
    for slot in ("second", "third", "fourth"):
        extractor.pages(path, "chars", range(1, 6), slot=slot)
        extractor.flush()
    files = list((tmp_path / "cache/pdf-pages/chars").iterdir())
    assert sum(file.stat().st_size for file in files) <= size * 2
    assert page_extraction._slot_path(tmp_path / "cache/pdf-pages", "chars", "fourth") in files


def test_extraction_errors_surface_as_without_cache(tmp_path):
    path = write(tmp_path, "a.pdf", text_pdf(PAGES))
    with pytest.raises(IndexError):
        PageExtractor(jobs=1).pages(path, "chars", [0])
    with pytest.raises(IndexError):
        PageExtractor(jobs=1).pages(path, "chars", [len(PAGES) + 1])


def test_bucketed_nearest_box_matches_ranking_every_box():
    generator = random.Random(7)
    for _ in range(200):
        nodes = []
        for _ in range(generator.randrange(0, 40)):
            x = generator.choice([generator.uniform(0, 600), 100.0])
            y = generator.choice([generator.uniform(0, 800), 300.0, 307.5])
            width, height, depth = generator.choice([(30.0, 8.0, 2.0), (generator.uniform(-40, 40), generator.uniform(0, 12), generator.uniform(0, 4))])
            box = (min(x, x + width), y - max(0, height), max(x, x + width), y + max(0, depth))
            nodes.append((generator.choice(["a.tex", "b.tex"]), generator.randrange(1, 6), box))
        if nodes and generator.random() < 0.3:
            nodes.append((nodes[0][0], nodes[0][1] + 1, nodes[0][2]))  # an exact tie
        index = _NodeIndex(nodes)
        for _ in range(60):
            x = generator.uniform(-20, 640)
            y = generator.choice([generator.uniform(-20, 820), 300.0, 306.0, 313.4, 316.0])
            assert index.nearest(x, y) == _ranked_nearest(nodes, x, y)


def test_region_text_matches_testing_every_character(tmp_path):
    from thoughts_server.renderer_tools.pdf_regions import PdfRegions

    write(tmp_path, "a.pdf", text_pdf(PAGES))
    generator = random.Random(3)
    with PdfRegions(tmp_path, PageExtractor(jobs=1)) as reader:
        for _ in range(100):
            page = generator.randrange(1, len(PAGES) + 1)
            regions = []
            for _ in range(generator.randrange(1, 4)):
                left, top = generator.uniform(0, 1), generator.uniform(0, 1)
                regions.append([left, top, left + generator.uniform(0, 0.6), top + generator.uniform(0, 0.3)])
            width, height, characters = reader.characters("a.pdf", page)
            selected = []
            for x0, x1, top, bottom, text in characters:
                x = (x0 + x1) / (2 * width)
                y = (top + bottom) / (2 * height)
                if any(a <= x <= c and b <= y <= d for a, b, c, d in regions):
                    selected.append((x0, top, text))
            selected.sort(key=lambda value: (round(value[1], 1), value[0]))
            assert reader.region_text("a.pdf", page, regions) == "".join(value[2] for value in selected)
        assert hashlib.sha256(reader.region_text("a.pdf", 1, [[0, 0, 1, 1]]).encode()).hexdigest()
        with pytest.raises(IndexError):
            reader.characters("a.pdf", 0)
        with pytest.raises(IndexError):
            reader.characters("a.pdf", len(PAGES) + 1)
        assert len(reader._characters) <= 8


PROPOSAL = (r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,"
            r"conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]")


def _document(edited_word: str) -> str:
    body = []
    for page in range(1, 4):
        body.append(f"\\section{{Part {page}}}")
        body.append(" ".join(f"Sentence {index} of part {page} explains the method." for index in range(40)))
        body.append(f"{PROPOSAL}{{edit-{page}}}{{reason}}{{Old {page}.}}{{New proposed text {page}.}}")
        if page == 2:
            body.append("\\ReviewCardMaskStart{0d1e2f30-4a5b-4c6d-8e9f-a0b1c2d3e4f5}{0}%\n"
                        f"The {edited_word} answer of the card.\n"
                        "\\ReviewCardMaskEnd{0d1e2f30-4a5b-4c6d-8e9f-a0b1c2d3e4f5}{0}%")
        body.append("\\newpage")
    return ("\\documentclass{article}\n\\usepackage{hyperref}\n\\begin{document}\n"
            + "\n\n".join(body) + "\n\\end{document}\n")


def test_publication_is_identical_uncached_parallel_stored_and_after_an_edit(tmp_path, monkeypatch):
    """A real compiled PDF: every extraction path publishes the same artifact."""
    import json
    import shutil

    from test_native_renderer import configure_current_fixture, prepare_native_corpus
    from thoughts_server import native_renderer, source_projection_cache
    from thoughts_server.project_documents import project_documents
    from thoughts_server.renderer_tools import publish_phone_source_bundle as bundle

    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real compiler required")
    monkeypatch.setenv("THOUGHTS_STRICT_REVIEW_CORRESPONDENCE", "false")
    monkeypatch.delenv("THOUGHTS_LATEX_CACHE_ROOT", raising=False)
    # Whole-document placement reuse would bypass the page extractions under test.
    monkeypatch.setattr(source_projection_cache, "restore", lambda location: None)
    docs, pdfs, artifacts = prepare_native_corpus(tmp_path)
    source = docs / "docs-manual/tex/source/main.tex"
    source.write_text(_document("plane"))
    configure_current_fixture(docs)
    target = project_documents(docs / "docs-manual")[0].output
    captured = tmp_path / "compiler-builds"
    original = native_renderer._publish_source_artifacts

    def capture(docs_root, artifact_root, outputs, builds_root, **kwargs):
        shutil.rmtree(captured, ignore_errors=True)
        shutil.copytree(builds_root, captured)
        return original(docs_root, artifact_root, outputs, builds_root, **kwargs)

    monkeypatch.setattr(native_renderer, "_publish_source_artifacts", capture)
    runs = iter(range(100))

    def publish(jobs, cache):
        if cache:
            monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
        else:
            monkeypatch.delenv("THOUGHTS_LATEX_CACHE_ROOT", raising=False)
        monkeypatch.setenv("THOUGHTS_COMPONENT_REVIEW_CACHE_ROOT", str(tmp_path / f"review-{next(runs)}"))
        extractor = PageExtractor(jobs=jobs)
        monkeypatch.setattr(page_extraction, "_default", extractor)
        output = tmp_path / f"artifacts-{next(runs)}"
        publisher = bundle.PhoneSourceBundlePublisher(
            docs, output, output / "ledger", publication_root=pdfs, builds_root=captured,
        )
        publisher.publish_document(target)
        pointer = json.loads((output / "documents" / f"{target}.json").read_bytes())
        objects = {path.name: path.read_bytes() for path in (output / "objects").iterdir()}
        return pointer["artifact_sha256"], objects, extractor.statistics

    # The edit keeps the document's glyph set: a font subset that gains or loses
    # a glyph changes every page that uses the font, and those pages miss.
    for word in ("plane", "panel"):
        source.write_text(_document(word))
        native_renderer.render(docs, pdfs, artifacts, targets=[target], jobs=1,
                               render_validation=False, prepare_comments=False)
        reference, objects, _ = publish(jobs=1, cache=False)
        manifest = json.loads(objects[reference])
        assert len(manifest["agent_proposals"]) == 3
        assert manifest["review_card_anchors"]
        assert manifest["pdf"]["basic_review"]["blocks"]
        statistics = {}
        for jobs, cache in ((3, True), (1, True), (3, False)):
            artifact, other_objects, statistics[jobs, cache] = publish(jobs, cache)
            assert (artifact, other_objects) == (reference, objects), (word, jobs, cache)
        # The parallel cached run stored every page; the next process reads them all.
        assert statistics[1, True]["computed"] == 0 and statistics[1, True]["stored"] > 0
        if word == "panel":
            # After the edit only the changed page was extracted again.
            assert statistics[3, True]["stored"] > 0, statistics
            assert statistics[3, True]["computed"] < statistics[3, False]["computed"], statistics

    # A new extraction implementation ignores every stored page.
    monkeypatch.setattr(page_extraction, "_IMPLEMENTATION", "e" * 64)
    artifact, other_objects, statistics = publish(1, True)
    assert artifact == reference and statistics["stored"] == 0
