# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Direct Markdown -> HTML -> PDF builds with verified, versioned evidence.

This renderer is deliberately separate from publication until all clients can
validate its proof type. It never fabricates SyncTeX files or LaTeX proofs.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, unquote_to_bytes, urlsplit

from .markdown_layout import ADAPTER_VERSION, WEASYPRINT_VERSION, layout_fragments, verify_layout
from .markdown_source import (
    PANDOC_VERSION,
    PROFILE,
    MarkdownError,
    digest,
    mapped_html,
    parse_markdown,
)

DEFAULT_CSS = b"""
@page { size: A4; margin: 20mm; }
body { font: 11pt/1.45 sans-serif; color: #20252b; }
h1, h2, h3 { line-height: 1.2; break-after: avoid; }
p { orphans: 2; widows: 2; }
code { font-family: monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
td, th { border: .5pt solid #bbb; padding: 5pt; vertical-align: top; overflow-wrap: anywhere; }
img { max-width: 100%; }
a + img { display: block; margin: 6pt 0; }
.thoughts-display-formula { display: block; text-align: center; margin: 12pt 0; }
"""
_ORIGIN = "https://thoughts-build.invalid/"


def _json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or str(path) != value
        or "\x00" in value
    ):
        raise MarkdownError(f"Dependency must have a normalized relative path: {value!r}")
    return value


@dataclass(frozen=True)
class MarkdownBuild:
    pdf: bytes
    manifest: dict
    blobs: dict[str, bytes]

    def write(self, output: Path) -> None:
        """Expose one complete build directory; never overwrite an existing build."""
        output = output.absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise MarkdownError(f"Build output already exists: {output}")
        staging = Path(tempfile.mkdtemp(prefix=".markdown-build-", dir=output.parent))
        try:
            (staging / "document.pdf").write_bytes(self.pdf)
            (staging / "manifest.json").write_bytes(_json(self.manifest))
            (staging / "blobs").mkdir()
            for sha, content in self.blobs.items():
                (staging / "blobs" / sha).write_bytes(content)
            # rename of a nonempty directory cannot replace another completed build.
            os.rename(staging, output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _xml_attribute_safe(svg: str) -> str:
    """MathJax serializes as HTML, which leaves "<" bare in attribute values.

    Its data-latex attribute holds the formula, so x_{<t} gave an SVG that is
    not well-formed XML and the build failed. Escape "<" inside quoted values.
    """
    return re.sub(r'="([^"]*)"', lambda m: '="' + m.group(1).replace("<", "&lt;") + '"', svg)


def _svg_font_longhands(data):
    """Work around WeasyPrint 70's size/family-only SVG font expander."""
    import xml.etree.ElementTree as ET

    import tinycss2
    from weasyprint.css.validation.expanders import expand_font

    def declarations(content):
        result = []
        for declaration in tinycss2.parse_declaration_list(content):
            if declaration.type != "declaration" or declaration.lower_name != "font":
                result.append(tinycss2.serialize([declaration]))
                continue
            tokens = [t for t in declaration.value if t.type not in {"whitespace", "comment"}]
            try:
                expanded = dict(expand_font.__wrapped__(tokens, "font"))
            except (ValueError, IndexError) as exc:
                raise MarkdownError("Unsupported SVG font shorthand") from exc
            defaults = {"-style": "normal", "-variant-caps": "normal", "-weight": "normal",
                        "-stretch": "normal", "line-height": "normal"}
            values = {**defaults, **{key: tinycss2.serialize(value) for key, value in expanded.items()}}
            important = " !important" if declaration.important else ""
            result.extend(f"{('font' + key) if key.startswith('-') else key}:{value}{important};"
                          for key, value in values.items())
        return "".join(result)

    root = ET.fromstring(data)
    changed = False
    for element in root.iter():
        if "style" in element.attrib and "font" in element.attrib["style"]:
            element.set("style", declarations(element.attrib["style"]))
            changed = True
        if element.tag == "{http://www.w3.org/2000/svg}style" and element.text:
            rules = tinycss2.parse_stylesheet(element.text)
            for rule in rules:
                if rule.type == "qualified-rule":
                    rule.content = tinycss2.parse_component_value_list(declarations(rule.content))
            element.text = tinycss2.serialize(rules)
            changed = True
    return ET.tostring(root) if changed else data


class _Snapshot:
    _fail_on_errors = False

    def __init__(self, dependencies, dependency_loader=None):
        self.dependencies = {_safe_path(key): bytes(value) for key, value in dependencies.items()}
        self.dependency_loader = dependency_loader
        self.blobs = {}
        self.records = {}
        self.errors = []

    def capture(self, name, content, kind):
        sha = digest(content)
        if name in self.records:
            if self.records[name]["sha256"] != sha:
                raise MarkdownError(f"A build dependency changed during rendering: {name}")
            return sha
        self.blobs[sha] = content
        self.records[name] = {"path": name, "sha256": sha, "size": len(content), "kind": kind}
        return sha

    def __call__(self, url):
        from weasyprint.urls import URLFetcherResponse

        try:
            parts = urlsplit(url)
            if parts.scheme == "data":
                header, payload = url[5:].split(",", 1)
                data = (
                    base64.b64decode(payload, validate=True)
                    if header.endswith(";base64")
                    else unquote_to_bytes(payload)
                )
                mime = header.split(";", 1)[0] or "text/plain"
                self.capture("data:" + digest(data), data, "embedded_asset")
            elif parts.scheme == "https" and parts.netloc == "thoughts-build.invalid" and not parts.query:
                path = _safe_path(unquote(parts.path.lstrip("/")))
                if path not in self.dependencies and self.dependency_loader is not None:
                    self.dependencies[path] = bytes(self.dependency_loader(path))
                if path not in self.dependencies:
                    raise MarkdownError(f"Dependency is missing from the build snapshot: {path}")
                data = self.dependencies[path]
                mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.capture(path, data, "asset")
            else:
                raise MarkdownError("Rendering may only read captured local assets: " + url[:180])
            if mime == "image/svg+xml":
                normalized = _svg_font_longhands(data)
                if normalized != data:
                    self.capture("renderer/svg-fonts/" + digest(normalized), normalized, "generated_asset")
                    data = normalized
            return URLFetcherResponse(url=url, body=data, headers={"Content-Type": mime})
        except Exception as exc:
            # WeasyPrint logs fetch errors and continues. The build must fail instead.
            self.errors.append(str(exc))
            raise


def _math_markup(fragments, snapshot, node_binary):
    requests = {key: fragment["math"] for key, fragment in fragments.items() if fragment["kind"] == "Math"}
    if not requests:
        return {}
    script = Path(__file__).with_name("markdown_math") / "render.mjs"
    try:
        completed = subprocess.run(
            [node_binary, str(script)],
            input=json.dumps(list(requests.values())),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        payload = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise MarkdownError("MathJax rendering failed; install the locked markdown_math runtime") from exc
    if payload.get("version") != "4.1.3" or len(payload.get("svgs", [])) != len(requests):
        raise MarkdownError("Unexpected MathJax runtime response")
    snapshot.capture(
        "renderer/mathjax-package-lock.json", script.with_name("package-lock.json").read_bytes(), "renderer"
    )
    snapshot.capture("renderer/mathjax-render.mjs", script.read_bytes(), "renderer")
    result = {}
    for (identifier, request), svg in zip(requests.items(), payload["svgs"], strict=True):
        # A space of \text{} is an empty path. It paints nothing, but the PDF
        # still holds it as a zero-size shape the drawing count below omits.
        svg = re.sub(r'<path\b[^>]*\bd=""[^>]*?(?:/>|>\s*</path>)', "", svg)
        svg = _xml_attribute_safe(svg)
        dims = {key: re.search(r"\b" + key + r'="([^"]+)"', svg) for key in ("width", "height")}
        if not all(dims.values()):
            raise MarkdownError("MathJax returned SVG without dimensions")
        vertical = re.search(r'vertical-align:\s*([^;" ]+)', svg)
        style = (
            f"width:{dims['width'].group(1)};height:{dims['height'].group(1)};"
            f"vertical-align:{vertical.group(1) if vertical else 'baseline'};"
        )
        uri = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        markup = (
            f'<img id="{identifier}" alt="{html.escape(request["tex"], quote=True)}" '
            f'src="{uri}" style="{html.escape(style, quote=True)}">'
        )
        if request["display"]:
            markup = '<span class="thoughts-display-formula">' + markup + "</span>"
        result[identifier] = markup
        fragments[identifier]["drawing_counts"] = {
            "curves": sum(len(re.findall(r"[Mm]", d)) for d in re.findall(r'<path\b[^>]*\bd="([^"]+)"', svg)),
            "rects": len(re.findall(r"<rect\b", svg)),
        }
    return result


def _semantic_links(ast, folder: str, source_path: str) -> None:
    """Give each relative link to another document the URI its LaTeX command would emit.

    The relative path is resolved here, against the document's place in the
    Library: the renderer's own base URL cannot express a path that leaves the
    source folder. Publication resolves the URIs like LaTeX links (doc_links).
    """
    from .markdown_links import classify

    def walk(value):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if value.get("t") == "Link":
                target = value["c"][2]
                link = classify(target[0], folder, source_path)
                if link is not None:
                    target[0] = link.uri()
            for item in value.values():
                walk(item)

    walk(ast)


def render_markdown(
    source: bytes,
    *,
    source_path="markdown/source/document.md",
    dependencies=None,
    dependency_loader=None,
    stylesheet: bytes = b"",
    stylesheet_path="markdown/support/document.css",
    node_binary="node",
    link_folder: str | None = None,
) -> MarkdownBuild:
    """Build from captured bytes. Network/file fetches outside that snapshot fail.

    A connected-folder loader may capture local dependencies on first use;
    subsequent layout passes reuse those exact bytes.
    ``link_folder`` is the docs-root-relative source folder of the document;
    with it, relative links to other documents become semantic links
    (markdown_links). Without it they stay plain relative links.
    All regions are normalized top-left (x0,y0,x1,y1); pages are zero based.
    Points use an explicit before/after affinity to a verified visible fragment,
    so they add no elements or whitespace and cannot change pagination.
    """
    try:
        import weasyprint
        from weasyprint.text.ffi import pango
        from weasyprint.text.fonts import get_hb_object_data
    except (ImportError, OSError) as exc:
        raise MarkdownError("Install the markdown-renderer extra and WeasyPrint's system libraries") from exc
    if weasyprint.__version__ != WEASYPRINT_VERSION:
        raise MarkdownError(f"The Markdown layout adapter requires WeasyPrint {WEASYPRINT_VERSION}")
    source_path, stylesheet_path = _safe_path(source_path), _safe_path(stylesheet_path)
    snapshot = _Snapshot(dependencies or {}, dependency_loader)
    paths = [source_path, stylesheet_path, *snapshot.dependencies]
    if source_path == stylesheet_path or any(path.startswith(("renderer/", "fonts/")) for path in paths):
        raise MarkdownError("Source, stylesheet, and reserved renderer snapshot paths must be distinct")
    for path in (source_path, stylesheet_path):
        if path in snapshot.dependencies:
            raise MarkdownError("Source/stylesheet must not also be passed as an asset")
    source = bytes(source)
    source_sha = snapshot.capture(source_path, source, "source")
    snapshot.capture("renderer/default.css", DEFAULT_CSS, "stylesheet")
    snapshot.capture(stylesheet_path, bytes(stylesheet), "stylesheet")
    snapshot.dependencies[stylesheet_path] = bytes(stylesheet)
    for module in ("markdown_source.py", "markdown_layout.py", "markdown_renderer.py"):
        snapshot.capture("renderer/" + module, Path(__file__).with_name(module).read_bytes(), "renderer")
    parsed = parse_markdown(source)
    if link_folder is not None:
        _semantic_links(parsed.ast, link_folder, source_path)
    math_html = _math_markup(parsed.fragments, snapshot, node_binary)
    actual_html, baseline_html, owners = mapped_html(parsed, math_html)

    def render(instrumented):
        fragment = actual_html if instrumented else baseline_html
        head = (
            '<meta charset="utf-8">'
            "<title>Thoughts document</title><style>" + DEFAULT_CSS.decode() + "</style>"
            f'<link rel="stylesheet" href="{html.escape(_ORIGIN + stylesheet_path, quote=True)}">'
        )
        # mapped_html supplies the actual body tree. Keep one HTML document and
        # insert the renderer styles in its head without changing that body.
        content = "<!doctype html>" + fragment.replace("</head>", head + "</head>", 1)
        return weasyprint.HTML(string=content, base_url=_ORIGIN + source_path, url_fetcher=snapshot).render()

    document = render(True)
    if snapshot.errors:
        raise MarkdownError("Incomplete build dependencies: " + "; ".join(sorted(set(snapshot.errors))))
    laid_out = layout_fragments(document, parsed.fragments, owners)
    pdf = document.write_pdf()
    baseline_pdf = render(False).write_pdf()
    if snapshot.errors:
        raise MarkdownError("Incomplete build dependencies: " + "; ".join(sorted(set(snapshot.errors))))
    font_faces = []
    for font in document.fonts.values():
        if font.missing:
            raise MarkdownError("A required glyph is missing from the rendering fonts")
        content = get_hb_object_data(font.hb_face)
        sha = snapshot.capture("fonts/" + digest(content), content, "font")
        face = {"sha256": sha, "index": font.index, "family": font.family}
        if face not in font_faces:
            font_faces.append(face)
    verified, validation = verify_layout(pdf, baseline_pdf, parsed.fragments, laid_out)
    # Dependency order is canonical. The input identity includes actual font bytes,
    # generated SVGs, renderer code and runtime versions, not just the Markdown hash.
    renderer = {
        "profile": PROFILE,
        "pandoc": PANDOC_VERSION,
        "weasyprint": WEASYPRINT_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "pango_version": pango.pango_version(),
        "font_faces": sorted(font_faces, key=lambda face: (face["sha256"], face["index"])),
        "packages": {
            name: version(name)
            for name in ("pydyf", "fonttools", "tinycss2", "cssselect2", "pdfplumber", "pypdf")
        },
    }
    dependency_records = sorted(snapshot.records.values(), key=lambda r: r["path"])
    input_sha = digest(_json({"renderer": renderer, "dependencies": dependency_records}))
    pdf_sha = digest(pdf)
    build_id = digest(_json({"input_sha256": input_sha, "pdf_sha256": pdf_sha}))
    marker_placements = {}
    ordered = sorted(
        parsed.fragments, key=lambda key: parsed.fragments[key]["source_ranges"][0]["byte_start"]
    )
    for key, marker in parsed.markers.items():
        if marker["kind"] == "range":
            locations = [
                {"fragment_id": identifier, **record}
                for identifier in parsed.fragments
                if key in parsed.fragments[identifier]["keys"]
                for record in verified[identifier]
            ]
        else:
            following = [
                identifier
                for identifier in ordered
                if parsed.fragments[identifier]["source_ranges"][0]["byte_start"] >= marker["byte_start"]
            ]
            if not ordered:
                raise MarkdownError("A point-only empty document has no visible source boundary")
            identifier, affinity = (following[0], "before") if following else (ordered[-1], "after")
            # A point before a wrapped token is on its first fragment; after EOF it
            # is on the last. Repeated headers do not duplicate the logical point.
            record = verified[identifier][0 if following else -1]
            rect = record["region"]
            locations = [
                {
                    "fragment_id": identifier,
                    "page_index": record["page_index"],
                    "point": [rect[0 if following else 2], (rect[1] + rect[3]) / 2],
                    "affinity": affinity,
                    "method": "markdown_source_boundary",
                }
            ]
        marker_placements[key] = {**marker, "locations": locations}
    manifest = {
        "schema_version": 1,
        "kind": "markdown_renderer_evidence",
        "source_format": "markdown",
        "source_path": source_path,
        "source_sha256": source_sha,
        "input_sha256": input_sha,
        "pdf_sha256": pdf_sha,
        "build_id": build_id,
        "renderer": renderer,
        "coordinate_system": "normalized_top_left",
        "page_index_base": 0,
        "dependencies": dependency_records,
        "validation": validation,
        "markers": marker_placements,
        "fragments": {key: {**info, "locations": verified[key]} for key, info in parsed.fragments.items()},
        "capabilities": {
            "text_fragments": True,
            "whole_equation_objects": True,
            "equation_symbol_selection": False,
            "publication_proof": False,
        },
    }
    return MarkdownBuild(pdf, manifest, snapshot.blobs)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build and verify a local Markdown PDF (no publication)")
    parser.add_argument("source", type=Path)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New build directory")
    parser.add_argument("--asset", type=Path, action="append", default=[])
    parser.add_argument("--stylesheet", type=Path)
    parser.add_argument("--node", default="node")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()

    def relative(path):
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise MarkdownError(f"Input is outside the project root: {path}") from exc

    try:
        assets = {relative(path): path.read_bytes() for path in args.asset}
        kwargs = {}
        if args.stylesheet:
            kwargs = {
                "stylesheet": args.stylesheet.read_bytes(),
                "stylesheet_path": relative(args.stylesheet),
            }
        result = render_markdown(
            args.source.read_bytes(),
            source_path=relative(args.source),
            dependencies=assets,
            node_binary=args.node,
            **kwargs,
        )
        result.write(args.output)
    except (MarkdownError, OSError) as exc:
        parser.exit(1, f"Markdown build failed: {exc}\n")
    print(
        json.dumps(
            {
                "status": "verified",
                "build_id": result.manifest["build_id"],
                "output": str(args.output.absolute()),
                **result.manifest["validation"],
            }
        )
    )


if __name__ == "__main__":
    main()
