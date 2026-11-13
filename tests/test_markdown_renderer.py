# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Run with the markdown-renderer extra and the locked MathJax runtime installed."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from thoughts_server.markdown_source import MarkdownError, SourcePositions, digest, parse_markdown

pytest.importorskip("pypandoc")
pytest.importorskip("weasyprint")

from thoughts_server.markdown_renderer import main, render_markdown  # noqa: E402


def marker(action, key="c1", segment="s1"):
    return f"<!-- thoughts:v1:{action} comment:{key}:{segment} -->"


def test_inline_equation_rectangular_path_survives_scaling():
    build = render_markdown('A small inline equation $0.198–$ continues here.'.encode())
    equation = next(f for f in build.manifest['fragments'].values() if f['kind'] == 'Math')
    assert equation['locations']


def test_image_hyperlink_keeps_label_and_renders_its_source_bound_figure():
    raw = b'---\ntitle: Hidden metadata\n---\n\n# Body\n\n[See figure](figure.png)\n'
    image = io.BytesIO()
    Image.new('RGB', (20, 20), 'red').save(image, format='PNG')
    build = render_markdown(raw, dependencies={'markdown/source/figure.png': image.getvalue()})
    pdf = PdfReader(io.BytesIO(build.pdf))
    assert 'See figure' in pdf.pages[0].extract_text()
    assert 'Hidden metadata' not in pdf.pages[0].extract_text()
    assert len(pdf.pages[0].images) == 1
    figure = next(f for f in build.manifest['fragments'].values() if f['kind'] == 'Image')
    assert figure['locations']
    assert raw[figure['source_ranges'][0]['byte_start']:figure['source_ranges'][0]['byte_end']] == b'[See figure](figure.png)'


def marked(text, key="c1", *, block=False):
    gap = "\n\n" if block else ""
    return marker("start", key) + gap + text + gap + marker("end", key)


def test_source_ranges_preserve_unicode_tabs_and_crlf():
    raw = ("é😀\t" + marked("**caffè** &amp; `é code`") + " end.\r\n").encode()
    parsed = parse_markdown(raw)
    wanted = {"é": "é", "😀": "😀", "caffè": "caffè", "&": "&amp;", "é code": "`é code`"}
    for fragment in parsed.fragments.values():
        spans = fragment["source_ranges"]
        for span in spans:
            assert digest(raw[span["byte_start"] : span["byte_end"]]) == span["sha256"]
        if fragment["text"] in wanted:
            assert raw[spans[0]["byte_start"] : spans[-1]["byte_end"]].decode() == wanted[fragment["text"]]
    comment = parsed.markers["comment:c1:s1"]
    assert raw[comment["byte_start"] : comment["byte_end"]] == "**caffè** &amp; `é code`".encode()
    positions = SourcePositions(b"a\tword\r\nsecond")
    assert positions.offset(1, 5) == 2
    assert positions.offset(2, 1) == 8
    with pytest.raises(MarkdownError):
        positions.offset(1, 3)


@pytest.mark.parametrize(
    "source",
    [
        marker("start") + "\n\nMissing end.",
        "Before " + marker("end") + " after.",
        "Before " + marked("one") + " " + marked("two"),
        "Before " + marked(" "),
        marker("start") + "prose on block marker line\n\n" + marker("end"),
        "Before <!-- thoughts:v2:start comment:c1:s1 -->unsupported.",
        "Before <!-- thoughts:v1:proposal {} -->unsupported.",
        "Before <span>raw HTML</span>.",
    ],
)
def test_invalid_or_unimplemented_syntax_fails_closed(source):
    with pytest.raises(MarkdownError):
        parse_markdown(source.encode())


def test_markers_inside_literal_code_are_never_comments():
    source = "`" + marker("start") + "`\n\n```html\n" + marker("end") + "\n```\n"
    parsed = parse_markdown(source.encode())
    assert parsed.markers == {}
    assert {f["kind"] for f in parsed.fragments.values()} == {"Code", "CodeBlock"}


def test_crossing_and_nested_ranges_use_original_offsets():
    source = (
        "Before "
        + marker("start", "a")
        + "alpha "
        + marker("start", "b")
        + marker("start", "nested")
        + "shared"
        + marker("end", "nested")
        + marker("end", "a")
        + " tail"
        + marker("end", "b")
        + " after."
    )
    build = render_markdown(source.encode())
    shared = next(f for f in build.manifest["fragments"].values() if f["text"] == "shared")
    assert set(shared["keys"]) == {"comment:a:s1", "comment:b:s1", "comment:nested:s1"}
    region = shared["locations"][0]["region"]
    assert all(
        region in [loc["region"] for loc in build.manifest["markers"][key]["locations"]]
        for key in shared["keys"]
    )


def test_long_inline_code_maps_every_line_and_page_without_changing_shaping():
    code = " ".join(["A long code fragment retains one source identity through every wrapped line."] * 18)
    source = "Before " + marked("`" + code + "`") + " after.\n\n" + marker("point", "end")
    build = render_markdown(source.encode(), stylesheet=b"@page {size: 260pt 220pt; margin: 22pt}")
    fragment = next(f for f in build.manifest["fragments"].values() if f["kind"] == "Code")
    locations = fragment["locations"]
    assert len(locations) > 10
    assert len({loc["page_index"] for loc in locations}) >= 2
    assert len([loc for loc in locations if loc["page_index"] == 0]) >= 3
    assert "".join("".join(loc["painted_text"].split()) for loc in locations) == "".join(code.split())
    assert all(0 <= v <= 1 for loc in locations for v in loc["region"])
    point = build.manifest["markers"]["comment:end:s1"]["locations"][0]
    assert point["affinity"] == "after"
    assert point["page_index"] == build.manifest["validation"]["pages"] - 1


def test_points_follow_source_identity_after_insertions_and_moves():
    source = (
        marker("point", "start") + "\n\nFirst paragraph.\n\nBefore " + marker("point", "inline") + "target."
    )
    before = render_markdown(source.encode())
    after = render_markdown(("Inserted content.\n\n" + source).encode())
    assert set(before.manifest["markers"]) == set(after.manifest["markers"])
    for key in before.manifest["markers"]:
        a = before.manifest["markers"][key]["locations"][0]
        b = after.manifest["markers"][key]["locations"][0]
        assert a["affinity"] == b["affinity"] == "before"
        assert a["point"] != b["point"]
    assert before.manifest["build_id"] != after.manifest["build_id"]


def test_repeated_table_headers_have_multiple_placements_for_one_source():
    table = "| " + marked("Header", "header") + " | Value |\n| --- | --- |\n"
    table += "\n".join(
        f"| Entry {i} | A sufficiently long table cell wraps onto more lines. |" for i in range(18)
    )
    build = render_markdown(table.encode(), stylesheet=b"@page {size: 280pt 300pt; margin: 25pt}")
    locations = build.manifest["markers"]["comment:header:s1"]["locations"]
    assert len({loc["page_index"] for loc in locations}) >= 3
    assert all(loc["painted_text"] == "Header" for loc in locations)


def test_native_link_remains_clickable_inside_marked_text():
    source = "Before " + marked("[an actual documentation link](https://pandoc.org/)") + " after."
    build = render_markdown(source.encode())
    annotations = PdfReader(io.BytesIO(build.pdf)).pages[0]["/Annots"]
    assert any(ref.get_object().get("/A", {}).get("/URI") == "https://pandoc.org/" for ref in annotations)


def test_math_is_whole_svg_objects_with_no_latex_process(monkeypatch):
    import subprocess

    runtime = Path(__file__).parents[1] / "src/thoughts_server/markdown_math/node_modules/@mathjax/src"
    if not runtime.exists():
        pytest.skip("Install the locked markdown_math runtime to run equation verification")
    original = subprocess.Popen
    commands = []

    def record(command, *args, **kwargs):
        commands.append(command)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record)
    build = render_markdown(
        (
            "Before "
            + marked("$x^2+y^2=z^2$", "inline")
            + " after.\n\n"
            + marked("$$\\frac{1}{3}$$", "display", block=True)
        ).encode()
    )
    assert all(
        loc["method"] == "markdown_svg_object"
        for marker_info in build.manifest["markers"].values()
        for loc in marker_info["locations"]
    )
    assert not build.manifest["capabilities"]["equation_symbol_selection"]
    assert all(not any("latex" in str(arg).lower() for arg in command) for command in commands)


def test_captures_assets_styles_fonts_and_source_in_atomic_build(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (40, 30), "#206080").save(image, format="PNG")
    dependencies = {
        "markdown/assets/plot.png": image.getvalue(),
        "markdown/support/colors.css": b"h1 {color: #206080}",
    }
    source = ("# Snapshot\n\n" + marked("![Plot](../assets/plot.png)", block=True)).encode()
    build = render_markdown(source, dependencies=dependencies, stylesheet=b'@import "colors.css";')
    records = {dep["path"]: dep for dep in build.manifest["dependencies"]}
    for path, data in dependencies.items():
        assert records[path]["sha256"] == digest(data)
    assert any(dep["kind"] == "font" for dep in records.values())
    assert build.manifest["source_sha256"] == digest(source)
    assert build.manifest["pdf_sha256"] == digest(build.pdf)
    output = tmp_path / "complete"
    build.write(output)
    assert json.loads((output / "manifest.json").read_text()) == build.manifest
    for record in records.values():
        assert digest((output / "blobs" / record["sha256"]).read_bytes()) == record["sha256"]
    with pytest.raises(MarkdownError, match="already exists"):
        build.write(output)


@pytest.mark.parametrize("target", ["https://example.com/image.png", "file:///etc/passwd", "../missing.png"])
def test_uncaptured_dependencies_never_produce_a_successful_build(target):
    with pytest.raises(MarkdownError):
        render_markdown(f"![missing]({target})".encode())


def test_missing_css_import_is_fatal_even_when_weasyprint_continues():
    with pytest.raises(MarkdownError, match="dependencies"):
        render_markdown(b"Text.", stylesheet=b'@import "missing.css";')


def test_version_mismatch_requires_adapter_revalidation(monkeypatch):
    import weasyprint

    monkeypatch.setattr(weasyprint, "__version__", "71.0")
    with pytest.raises(MarkdownError, match="requires WeasyPrint"):
        render_markdown(b"Text.")


def test_cli_rejects_incomplete_build_without_output(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("![missing](missing.png)")
    output = tmp_path / "output"
    with pytest.raises(SystemExit) as error:
        main([str(source), "--project-root", str(tmp_path), "--output", str(output)])
    assert error.value.code == 1
    assert not output.exists()


def test_authored_destinations_survive_source_mapping():
    from thoughts_server.markdown_source import mapped_html

    parsed = parse_markdown(b"```{#mycode}\ncode\n```\n\n![picture](plot.png){#myimage}\n")
    actual, baseline, _ = mapped_html(parsed, {})
    for markup in (actual, baseline):
        assert 'id="mycode"' in markup
        assert 'id="myimage"' in markup
        assert '<span' not in markup


def test_source_label_heading_creates_pdf_destination_and_rejects_duplicates():
    build = render_markdown(b"# Heading {#source-label:result}\n\nBody text.\n")
    assert "source-label:result" in PdfReader(io.BytesIO(build.pdf)).named_destinations
    with pytest.raises(MarkdownError, match="Duplicate Source label"):
        parse_markdown(b"# First {#source-label:result}\n\n# Second {#source-label:result}\n")


@pytest.mark.parametrize("path", ["renderer/default.css", "fonts/something", "../outside.md"])
def test_source_cannot_collide_with_internal_dependency_records(path):
    with pytest.raises(MarkdownError):
        render_markdown(b"Text.", source_path=path)


def test_no_word_wrappers_remain_to_break_kerning_or_ligatures():
    from thoughts_server.markdown_source import mapped_html

    parsed = parse_markdown(("office after. Before " + marked("a passage") + " after.").encode())
    actual, baseline, _ = mapped_html(parsed, {})
    assert "<span" not in actual
    assert "office after. Before a passage after." in actual
    assert "data-thoughts" not in baseline
    build = render_markdown(("office after. Before " + marked("a passage") + " after.").encode())
    assert build.manifest["validation"]["unchanged_text_characters"] > 20


@pytest.mark.parametrize('width', [80, 240])
def test_svg_drawings_text_and_embedded_raster_are_verified_at_rendered_size(width):
    import base64

    image = io.BytesIO()
    Image.new('RGB', (4, 4), 'red').save(image, format='PNG')
    uri = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"
        viewBox="0 0 240 120">
        <g transform="translate(5 5)"><rect width="200" height="100" fill="white"/>
        <path d="M0 0 L100 80" stroke="black"/><circle cx="150" cy="50" r="20"/>
        <text x="5" y="30">SVG label</text>
        <image href="{uri}" x="10" y="60" width="20" height="20"/></g></svg>'''.encode()
    raw = b'Before.\n\n![diagram](figure.svg)\n\nAfter.\n\n![repeat](figure.svg)\n'
    build = render_markdown(raw, dependencies={'markdown/source/figure.svg': svg},
                            stylesheet=f'img {{ width: {width}px; }}'.encode())
    figures = [f for f in build.manifest['fragments'].values() if f['kind'] == 'Image']
    assert len(figures) == 2
    for figure in figures:
        assert figure['locations'][0]['method'] == 'markdown_svg_object'
        assert 'SVGlabel' in figure['locations'][0]['painted_text']
    assert any(d['path'] == 'markdown/source/figure.svg' for d in build.manifest['dependencies'])


def test_empty_svg_cannot_borrow_nearby_text_as_evidence():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"/>'
    with pytest.raises(MarkdownError, match='no verifiable PDF drawing'):
        render_markdown(b'Nearby text.\n\n![empty](empty.svg)',
                        dependencies={'markdown/source/empty.svg': svg})


def test_svg_missing_paint_is_rejected(monkeypatch):
    import thoughts_server.markdown_renderer as renderer

    original = renderer.verify_layout

    other = render_markdown(b'Unrelated text.').pdf

    def corrupt(pdf, baseline, fragments, records):
        for record in records:
            if record.get('svg_pdf'):
                record['svg_pdf'] = other
        return original(pdf, baseline, fragments, records)

    monkeypatch.setattr(renderer, 'verify_layout', corrupt)
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
           b'<rect width="90" height="90"/></svg>')
    with pytest.raises(MarkdownError, match='SVG drawing membership mismatch'):
        render_markdown(b'![diagram](figure.svg)', dependencies={'markdown/source/figure.svg': svg})


def test_svg_font_shorthand_keeps_weight_and_size():
    import pdfplumber

    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="100">
      <style>.label { font: 700 20px Arial, sans-serif; }</style>
      <text class="label" x="10" y="30">AGENT</text>
      <text style="font: italic 12px Arial, sans-serif" x="10" y="60">model</text>
      </svg>'''
    build = render_markdown(b'![diagram](figure.svg)', dependencies={'markdown/source/figure.svg': svg})
    with pdfplumber.open(io.BytesIO(build.pdf)) as pdf:
        chars = pdf.pages[0].chars
        assert [c['size'] for c in chars[:5]] == pytest.approx([15] * 5)
        assert [c['size'] for c in chars[5:]] == pytest.approx([9] * 5)
    assert any(d['kind'] == 'generated_asset' for d in build.manifest['dependencies'])


def test_wide_table_wraps_with_every_source_fragment_inside_page():
    headings = ["Reviewer", "Soundness", "Presentation", "Contribution", "Rating", "Confidence"]
    source = "| " + " | ".join(headings) + " |\n|" + "---|" * len(headings) + "\n"
    source += "| " + " | ".join(["LongUnbrokenValue" * 4] * len(headings)) + " |\n"
    build = render_markdown(source.encode(), stylesheet=b"@page {size: 420pt 595pt; margin: 30pt}")
    assert build.manifest["fragments"]
    for fragment in build.manifest["fragments"].values():
        assert fragment["locations"]
        for location in fragment["locations"]:
            x0, y0, x1, y1 = location["region"]
            assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def test_spaces_inside_equation_text_draw_nothing_and_verify():
    runtime = Path(__file__).parents[1] / "src/thoughts_server/markdown_math/node_modules/@mathjax/src"
    if not runtime.exists():
        pytest.skip("Install the locked markdown_math runtime to run equation verification")
    # MathJax draws each space of \text{} as an empty path.
    build = render_markdown(b"Chunk $p(\\text{ld} \\mid \\text{Hel, lo, wor})$ ends here.")
    equation = next(f for f in build.manifest["fragments"].values() if f["kind"] == "Math")
    assert equation["locations"]


def test_text_presentation_selector_is_not_source_text():
    # Pandoc writes U+FE0E after an arrow that also has an emoji form.
    build = render_markdown("Chunk ① \"Hello\" ↔ Hel·lo.".encode())
    arrow = next(f for f in build.manifest["fragments"].values() if "↔" in f.get("text", ""))
    assert arrow["locations"]
