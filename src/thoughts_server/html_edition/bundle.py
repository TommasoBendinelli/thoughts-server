# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Turn a converter's HTML into a self-contained, script-free HTML edition.

Shared by the LaTeX (make4ht/tex4ht) and Markdown (Pandoc) converters:

- math (MathML from tex4ht, TeX from Pandoc) -> inline MathJax 4.1.3 SVG (currentColor);
- marker anchors -> ``<span class="tm" data-tm="start|end|point" data-key="..."></span>``;
- tex4ht paragraph line spans -> ``data-src-lines="<file>:<first>-<last>"`` on blocks;
- document links (manual-*: and thoughts-html:) -> ``thoughts-link:`` hrefs that also
  carry ``data-thoughts-link``; ``\\SourceLabel`` -> ``id="source-label:<label>"``;
- images -> ``assets/<sha256 prefix>.<ext>`` (PDF figures -> SVG);
- no scripts, no external references; ``validate`` enforces it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

# lxml is installed with pikepdf (a server dependency); uv.lock already pins it.
import lxml.html
from lxml import etree

from . import ENTRY, STYLESHEET

MATH_RUNTIME = Path(__file__).resolve().parent.parent / "markdown_math"
MATH_SCRIPT = MATH_RUNTIME / "render-edition.mjs"

BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "figure", "figcaption", "table", "tr",
              "td", "th", "dd", "dt", "blockquote", "div", "section", "caption"}
LINE_TARGETS = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "td", "th", "figcaption", "dd", "dt")
LINE_TARGET_XPATH = " or ".join(f"self::{tag}" for tag in LINE_TARGETS)
ANCHOR_SPAN = re.compile(r'<span (?=[^>]*class=["\']tm["\'])[^>]*></span>')
MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_VECTOR_FIGURE_BYTES = 1536 * 1024

READING_CSS = """
/* Thoughts reading stylesheet: reflows to the window, follows light and dark mode. */
:root { --thoughts-fg:#1b1b1b; --thoughts-bg:#ffffff; --thoughts-muted:#666; --thoughts-link:#1a5fb4;
        --thoughts-rule:#d0d0d0; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --thoughts-fg:#e6e6e6; --thoughts-bg:#151515; --thoughts-muted:#aaa;
                                    --thoughts-link:#8ab4f8; --thoughts-rule:#444; }
}
:root[data-theme="dark"] { --thoughts-fg:#e6e6e6; --thoughts-bg:#151515; --thoughts-muted:#aaa;
                           --thoughts-link:#8ab4f8; --thoughts-rule:#444; }
html { background: var(--thoughts-bg); color: var(--thoughts-fg); }
body { max-width: 46em; margin: 0 auto; padding: 1em 16px 4em; font: 17px/1.55 "Latin Modern Roman", Georgia, serif;
       background: var(--thoughts-bg); color: var(--thoughts-fg); overflow-wrap: break-word; }
a { color: var(--thoughts-link); }
img, svg.figure { max-width: 100%; height: auto; }
.m-display { display: block; overflow-x: auto; overflow-y: hidden; text-align: center; margin: .6em 0; }
.m-display > svg { max-width: none; }
.m-error { font-family: monospace; color: var(--thoughts-muted); }
pre, code, .verbatim { font-family: "Latin Modern Mono", Menlo, monospace; font-size: .9em; }
pre { overflow-x: auto; white-space: pre-wrap; }
table { border-collapse: collapse; max-width: 100%; overflow-x: auto; display: block; }
table.equation, table.equation-star { display: table; width: 100%; }
td, th { padding: .15em .5em; vertical-align: top; }
.paragraphHead, .subparagraphHead { margin-right: .6em; }
figure, div.figure { text-align: center; margin: 1em 0; }
hr { border: 0; border-top: 1px solid var(--thoughts-rule); }
.tm { display: inline; }
.thoughts-link-inactive { color: var(--thoughts-muted); }
svg { color: inherit; }
"""


class EditionInvalid(ValueError):
    """The edition would contain a script, an external reference or a broken file."""


@dataclass
class LinkContext:
    """What a document link needs to name its target the way the PDF link does."""

    folder: str                      # the document's source folder (docs-x or .thoughts/projects/<h>/default)
    source_path: str                 # folder-relative path of the document's main source
    docs_root: Path | None = None    # a workspace with the Project manifests, for target filenames


@dataclass
class RawEdition:
    """A converter's output, before the shared bundle step."""

    html: str
    source_format: str               # latex | markdown
    source_map: str                  # exact | lines | none
    converter: dict
    links: LinkContext
    math: str = "mml"                # mml: tex4ht MathML; none: math already rendered
    stylesheets: list[bytes] = field(default_factory=list)
    resolve_image: Callable[[str], Path | None] = lambda _src: None
    line_root: Path | None = None    # tex4ht line spans: paths relative to this root
    line_prefix: str = ""            # ... and prefixed with this docs-root-relative folder
    warnings: list[str] = field(default_factory=list)
    source_files: dict[str, str] = field(default_factory=dict)   # data-src file -> SHA-256
    run_tool: Callable[[list[str], Path], subprocess.CompletedProcess] | None = None


@dataclass
class Edition:
    files: dict[str, bytes]
    anchors: list[str]
    warnings: list[str]
    stats: dict
    source_format: str = ""
    source_map: str = "none"
    converter: dict = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ math

def render_math(requests: list[dict], *, node: str = "node", timeout: int = 300) -> tuple[list, list]:
    """``[{"kind": "tex"|"mml", "src", "display"}]`` -> (svgs, errors), one per request."""
    if not requests:
        return [], []
    if not (MATH_RUNTIME / "node_modules" / "@mathjax" / "src").is_dir():
        raise RuntimeError("the locked MathJax runtime is not installed (npm ci in markdown_math)")
    completed = subprocess.run(
        ["nice", "-n", "19", node, str(MATH_SCRIPT)], input=json.dumps(requests).encode(),
        capture_output=True, timeout=timeout, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("MathJax failed: " + completed.stderr.decode("utf-8", "replace")[-300:])
    payload = json.loads(completed.stdout)
    if payload.get("version") != "4.1.3" or len(payload.get("svgs", [])) != len(requests):
        raise RuntimeError("unexpected MathJax runtime response")
    return payload["svgs"], payload["errors"]


def math_wrapper(svg: str | None, display: bool, fallback: str) -> etree._Element:
    wrapper = etree.Element("span")
    wrapper.set("class", "m-display" if display else "m")
    if svg is None:
        wrapper.set("class", wrapper.get("class") + " m-error")
        wrapper.text = fallback
    else:
        wrapper.append(lxml.html.fragment_fromstring(svg))
    return wrapper


def _clean_mathml(doc) -> None:
    """tex4ht MathML clean-up before MathJax (operator spacing, upright function names)."""
    for comment in doc.xpath("//math//comment()"):
        _remove_keep_tail(comment)
    for mstyle in doc.xpath("//math//mstyle[not(@*)]"):
        mstyle.drop_tag()
    for element in doc.xpath('//math//*[contains(@class, "loglike")]'):
        for mi in ([element] if element.tag == "mi" else []) + element.xpath(".//mi"):
            mi.text = unicodedata.normalize("NFKC", mi.text or "")
            mi.attrib.pop("mathvariant", None)
        if element.tag == "mstyle":
            element.drop_tag()


def _remove_keep_tail(node) -> None:
    parent = node.getparent()
    if node.tail:
        previous = node.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + node.tail
        else:
            parent.text = (parent.text or "") + node.tail
    parent.remove(node)


# ------------------------------------------------------------------ links

def _query(**values: str | None) -> str:
    return "&".join(f"{name}={quote(value, safe='/')}" for name, value in values.items() if value is not None)


def _source_target(selector: str, label: str, docs_root: Path | None) -> dict:
    from ..renderer_tools import doc_links

    target = {"path": selector, "label": label}
    with _suppress():
        if label == "document-start":
            target["destination"] = doc_links.canonical_tex_destination_name(selector, label)
        elif label.startswith(doc_links.SOURCE_LABEL_PREFIX):
            target["destination"] = label
    if docs_root is not None:
        with _suppress():
            resolved = doc_links.target_for_selector(docs_root, selector)
            target["filename"] = resolved.pdf_path.name
    return target


class _quiet:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, (OSError, subprocess.SubprocessError))


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, Exception)


def thoughts_link(href: str, context: LinkContext) -> tuple[str | None, str, str | None]:
    """(thoughts-link URI or None, kind, problem) for a document link; kind "" = not one."""
    from ..renderer_tools import doc_links

    if href.startswith("thoughts-html:"):
        path = unquote(href[len("thoughts-html:"):]).lstrip("/")
        if not path.startswith("docs-") and not path.startswith(".thoughts/"):
            path = f"{context.folder}/{path}"
        return "thoughts-link:html?" + _query(path=path), "html", None
    if href.startswith(doc_links.MARKDOWN_INVALID_LINK_URI_PREFIX):
        problem = unquote(href.partition(doc_links.MARKDOWN_INVALID_LINK_PROBLEM_SEPARATOR)[2])
        return None, "invalid", problem or "not a link to a document"
    try:
        if (cross_pdf := doc_links.parse_cross_pdf_link_uri(href)) is not None:
            owner, key, anchor = cross_pdf
            return ("thoughts-link:cross-pdf?" + _query(folder=owner, key=key, anchor=anchor,
                                                         filename=f"{key}.pdf"), "cross-pdf", None)
        if (pdf := doc_links.parse_pdf_link_uri(href)) is not None:
            key, anchor = pdf
            return ("thoughts-link:pdf?" + _query(folder=context.folder, key=key, anchor=anchor,
                                                   filename=f"{key}.pdf"), "pdf", None)
        source = None
        kind = ""
        if href.startswith(doc_links.SOURCE_LINK_URI_PREFIX):
            source, kind = doc_links.parse_source_link_uri(href, context.folder), "source"
        elif href.startswith(doc_links.CROSS_SOURCE_LINK_URI_PREFIX):
            source, kind = doc_links.parse_cross_source_link_uri(href), "cross-source"
    except ValueError as error:
        return None, "invalid", str(error)
    if source is None:
        return None, "", None
    target = _source_target(source[0], source[1], context.docs_root)
    return "thoughts-link:" + kind + "?" + _query(**target), kind, None


def _relative_link(href: str, context: LinkContext) -> tuple[str | None, str]:
    """A plain relative link of a Markdown source: an .html attachment, or nothing."""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:|^//|^#", href):
        return None, ""
    path = unquote(href.partition("#")[0].partition("?")[0])
    if not path:
        return None, ""
    base = PurePosixPath(context.folder) / PurePosixPath(context.source_path).parent
    parts: list[str] = []
    for part in (base / path).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    resolved = "/".join(parts)
    if PurePosixPath(resolved).suffix.lower() in {".html", ".htm"}:
        return "thoughts-link:html?" + _query(path=resolved), "html"
    return None, "relative"


# ------------------------------------------------------------------ bundle

class _Files:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def add(self, name: str, data: bytes) -> str:
        self.files[name] = data
        return name

    def asset(self, data: bytes, extension: str) -> str:
        return self.add(f"assets/{sha256(data)[:16]}{extension}", data)


def _source_lines(doc, raw: RawEdition, stats: dict) -> None:
    for sl in doc.xpath('//math//span[@class="sl"]'):
        _remove_keep_tail(sl)
    blocks: dict = {}
    for sl in doc.xpath('//span[@class="sl"]'):
        kind, _, rest = sl.get("data-sl", "").partition(":")
        file, _, line = rest.rpartition(":")
        target = _block_of(sl)
        loose = target is None or target.tag in {"body", "div", "section"}
        if kind == "t":
            following = sl.xpath("following::table[1]")
            target = following[0] if following else target
        elif kind in ("b", "h"):
            # TeX starts the next paragraph before the converter closes the current
            # block: a begin span with no text after it inside its block belongs
            # to the next block in document order.
            after = False
            for text in sl.xpath("following::text()"):
                owner = text.getparent()
                if not text.is_text:
                    owner = owner.getparent()
                if owner is None or not (owner is target or target in owner.iterancestors()):
                    break
                if text.strip():
                    after = True
                    break
            if loose or not after:
                following = sl.xpath(f"following::*[{LINE_TARGET_XPATH}][1]")
                target = following[0] if following else target
        elif loose:
            preceding = sl.xpath(f"preceding::*[{LINE_TARGET_XPATH}][1]")
            target = preceding[0] if preceding else target
        relative = _line_file(file, raw)
        if target is not None and target.tag != "body" and line.isdigit() and relative:
            blocks.setdefault(target, []).append((relative, int(line)))
        _remove_keep_tail(sl)
    for block, entries in blocks.items():
        first_file = entries[0][0]
        lines = [line for file, line in entries if file == first_file]
        block.set("data-src-lines", f"{first_file}:{min(lines)}-{max(lines)}")


def _line_file(file: str, raw: RawEdition) -> str | None:
    if not file or raw.line_root is None:
        return None
    path = Path(file)
    if not path.is_absolute():
        path = raw.line_root / path
    try:
        relative = Path(os.path.normpath(path)).relative_to(raw.line_root.resolve()).as_posix()
    except ValueError:
        try:
            relative = Path(os.path.normpath(path)).relative_to(raw.line_root).as_posix()
        except ValueError:
            return None
    return f"{raw.line_prefix}/{relative}" if raw.line_prefix else relative


def _block_of(element):
    node = element.getparent()
    while node is not None and node.tag not in BLOCK_TAGS and node.tag != "body":
        node = node.getparent()
    return node


def _normalise_anchors(doc, warnings: list[str]) -> None:
    hoisted = 0
    for anchor in doc.xpath('//span[contains(concat(" ", normalize-space(@class), " "), " tm ")]'):
        # tex4ht may place the following space inside the (empty) span: move it out.
        if anchor.text or len(anchor):
            anchor.tail = (anchor.text_content() or "") + (anchor.tail or "")
            anchor.text = None
            for child in list(anchor):
                anchor.remove(child)
        math = next((a for a in anchor.iterancestors() if a.tag == "math"), None)
        if math is not None:
            anchor.getparent().remove(anchor)
            math.addprevious(anchor)
            anchor.tail = None
            hoisted += 1
        for name in list(anchor.attrib):
            if name not in {"class", "data-tm", "data-key"}:
                del anchor.attrib[name]
        anchor.set("class", "tm")
    if hoisted:
        warnings.append(f"{hoisted} markers were inside math; moved before the formula")


def build_edition(raw: RawEdition, *, node: str = "node") -> Edition:
    """Bundle a converter's HTML; raises EditionInvalid if the result is not self-contained."""
    files = _Files()
    warnings = list(raw.warnings)
    stats: dict = {}
    doc = lxml.html.document_fromstring(raw.html)
    body = doc.find("body")
    if body is None:
        raise EditionInvalid("the converter produced no document body")

    _normalise_anchors(doc, warnings)
    if raw.line_root is not None:
        _source_lines(doc, raw, stats)

    # Math -> SVG.
    requests, nodes = [], []
    if raw.math == "mml":
        _clean_mathml(doc)
        for math in doc.iter("math"):
            if any(ancestor.tag == "math" for ancestor in math.iterancestors()):
                continue
            xml = etree.tostring(math, encoding="unicode", with_tail=False)
            if "xmlns" not in xml[:80]:
                xml = xml.replace("<math", '<math xmlns="http://www.w3.org/1998/Math/MathML"', 1)
            requests.append({"kind": "mml", "src": xml, "display": math.get("display") == "block"})
            nodes.append(math)
    for element in doc.xpath('//*[@data-thoughts-tex]'):
        requests.append({"kind": "tex", "src": element.get("data-thoughts-tex"),
                         "display": element.get("data-display") == "block"})
        nodes.append(element)
    svgs, errors = render_math(requests, node=node)
    failed = 0
    for element, svg, request in zip(nodes, svgs, requests, strict=True):
        fallback = request["src"] if request["kind"] == "tex" else element.text_content()
        wrapper = math_wrapper(svg, request["display"], fallback)
        identifier = element.get("id")
        if identifier and request["kind"] == "tex":
            wrapper.set("id", identifier)
        if element.get("data-src"):
            wrapper.set("data-src", element.get("data-src"))
        failed += svg is None
        wrapper.tail = element.tail
        element.getparent().replace(element, wrapper)
    stats["math"] = len(requests)
    stats["math_failed"] = failed
    if failed:
        samples = list(dict.fromkeys(error for error in errors if error))[:3]
        warnings.append(f"{failed} of {len(requests)} formulas could not be rendered: " + "; ".join(samples))

    # Source-map coverage (text blocks with a source position).
    text_blocks = [block for block in body.iter(*LINE_TARGETS) if (block.text_content() or "").strip()]
    if raw.source_map == "lines":
        covered = sum(1 for block in text_blocks if block.get("data-src-lines")
                      or any(a.get("data-src-lines") for a in block.iterancestors()))
    else:
        covered = sum(1 for block in text_blocks if block.get("data-src") or block.xpath(".//*[@data-src]"))
    stats["text_blocks"] = len(text_blocks)
    stats["text_blocks_mapped"] = covered

    # Links.
    link_counts: dict[str, int] = {}
    inactive = []
    for anchor in list(doc.iter("a")):
        href = anchor.get("href")
        if not href:
            continue
        if href.startswith(ENTRY + "#"):
            # tex4ht names its own (single) output file in cross-references.
            href = href[len(ENTRY):]
            anchor.set("href", href)
        uri, kind, problem = thoughts_link(href, raw.links)
        if not kind and raw.source_format == "markdown":
            uri, kind = _relative_link(href, raw.links)
        if uri is not None:
            anchor.set("href", uri)
            anchor.set("data-thoughts-link", uri)
            anchor.set("class", (anchor.get("class", "") + " thoughts-link").strip())
            link_counts[kind] = link_counts.get(kind, 0) + 1
        elif kind in {"invalid", "relative"}:
            # A link the PDF also leaves inactive, or a file outside the edition.
            anchor.attrib.pop("href", None)
            anchor.set("class", (anchor.get("class", "") + " thoughts-link-inactive").strip())
            if problem:
                anchor.set("title", problem)
            inactive.append(href)
        elif re.match(r"^(https?:|mailto:)", href, re.I):
            anchor.set("rel", "noopener noreferrer")
        elif href.startswith("#"):
            pass
        else:
            anchor.attrib.pop("href", None)
            inactive.append(href)
    stats["links"] = link_counts
    if inactive:
        warnings.append(f"{len(inactive)} links have no target in the HTML view: " + ", ".join(inactive[:3]))
    for element in doc.xpath("//*[@data-source-label]"):
        element.set("id", "source-label:" + element.attrib.pop("data-source-label"))
    stats["source_labels"] = len(doc.xpath('//*[starts-with(@id, "source-label:")]'))

    # Images.
    bundled = missing = 0
    missing_names = []
    for image in list(doc.iter("img")):
        source = image.get("src", "")
        if source.startswith("data:image/") and not source.startswith("data:image/svg"):
            continue
        out_missing: list[str] = []
        name = _bundle_image(source, raw, files, out_missing)
        if name is None:
            missing += 1
            missing_names.append(source + (f" ({out_missing[0]})" if out_missing else ""))
            image.attrib.pop("src", None)
            image.set("alt", image.get("alt") or "figure unavailable")
            image.set("class", (image.get("class", "") + " thoughts-missing-figure").strip())
            continue
        image.set("src", name)
        for attribute in ("width", "height", "srcset"):
            image.attrib.pop(attribute, None)
        size = intrinsic_size(files.files[name], name)
        if size is not None:
            # Intrinsic size: the page reserves the figure's space before it loads.
            image.set("width", str(size[0]))
            image.set("height", str(size[1]))
        options = image.attrib.pop("data-opts", "")
        width = re.search(r"width\s*=\s*([0-9.]*)\s*\\(linewidth|textwidth|columnwidth|hsize)", options)
        if width:
            image.set("style", f"width:{min(100.0, float(width.group(1) or 1) * 100):.0f}%")
        bundled += 1
    stats["images"] = bundled
    stats["images_missing"] = missing
    if missing:
        warnings.append(f"{missing} figures are not in the HTML view: " + ", ".join(missing_names[:3]))

    # No scripts or external references; one stylesheet.
    for element in doc.xpath("//script | //link | //iframe | //frame | //object | //embed | //meta | //title"
                             " | //base | //noscript | //form | //style"):
        element.getparent().remove(element)
    for element in doc.iter():
        if not isinstance(element.tag, str):
            continue
        for name in list(element.attrib):
            if name.lower().startswith("on"):
                del element.attrib[name]
    head = doc.find("head")
    if head is None:
        head = etree.Element("head")
        doc.insert(0, head)
    charset = etree.SubElement(head, "meta")
    charset.set("charset", "utf-8")
    viewport = etree.SubElement(head, "meta")
    viewport.set("name", "viewport")
    viewport.set("content", "width=device-width, initial-scale=1")
    link = etree.SubElement(head, "link")
    link.set("rel", "stylesheet")
    link.set("href", STYLESHEET)
    css = "\n".join(sheet.decode("utf-8", "replace") for sheet in raw.stylesheets)
    css = re.sub(r"@import[^;]*;", "", css)
    css = re.sub(r"url\(\s*(['\"]?)(?!data:|#)[^)]*\)", "none", css)
    files.add(STYLESHEET, (css + "\n" + READING_CSS).encode())

    html = "<!DOCTYPE html>\n" + lxml.html.tostring(doc, encoding="unicode")

    def canonical(match):
        attributes = dict(re.findall(r'([\w-]+)=["\']([^"\']*)["\']', match.group(0)))
        return (f'<span class="tm" data-tm="{attributes.get("data-tm")}" '
                f'data-key="{attributes.get("data-key")}"></span>')

    html = ANCHOR_SPAN.sub(canonical, html)
    files.add(ENTRY, html.encode())
    anchors = list(dict.fromkeys(
        match.group(2) for match in re.finditer(r'<span class="tm" data-tm="(start|end|point)" data-key="([^"]*)">', html)
    ))
    stats["anchor_spans"] = len(re.findall(r'<span class="tm" ', html))
    validate(files.files)
    stats["files"] = len(files.files)
    stats["bytes"] = sum(len(data) for data in files.files.values())
    return Edition(files.files, anchors, warnings, stats, raw.source_format, raw.source_map, raw.converter,
                   raw.source_files)


_CSS_UNITS = {"": 1.0, "px": 1.0, "pt": 4 / 3, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4,
              "em": 16.0, "ex": 8.0}


def _length(value: str | None) -> float | None:
    match = re.fullmatch(r"\s*([0-9.]+)\s*(px|pt|pc|in|cm|mm|em|ex)?\s*", value or "")
    if not match:
        return None
    return float(match.group(1)) * _CSS_UNITS[match.group(2) or ""]


def intrinsic_size(data: bytes, name: str) -> tuple[int, int] | None:
    """(width, height) in CSS px of a bundled image, or None if unknown."""
    try:
        if name.endswith(".svg"):
            root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True,
                                                          huge_tree=True))
            width, height = _length(root.get("width")), _length(root.get("height"))
            if not (width and height) and root.get("viewBox"):
                parts = [float(part) for part in re.split(r"[\s,]+", root.get("viewBox").strip())]
                width, height = parts[2], parts[3]
        else:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
    except Exception:  # noqa: BLE001 - a size is an optimisation, never a failure
        return None
    if not width or not height or width <= 0 or height <= 0:
        return None
    return max(1, round(width)), max(1, round(height))


def _bundle_image(source: str, raw: RawEdition, files: _Files, problems: list[str]) -> str | None:
    if source.startswith("data:image/svg"):
        problems.append("inline SVG data")
        return None
    path = raw.resolve_image(source)
    if path is None:
        problems.append("not found")
        return None
    if path.stat().st_size > MAX_ASSET_BYTES:
        problems.append("too large")
        return None
    extension = path.suffix.lower()
    if extension == ".pdf":
        with tempfile.TemporaryDirectory(prefix="thoughts-figure-") as temporary:
            output = Path(temporary) / "figure.svg"
            command = ["pdftocairo", "-svg", "-f", "1", "-l", "1", str(path), str(output)]
            try:
                result = (raw.run_tool(command, Path(temporary)) if raw.run_tool is not None
                          else subprocess.run(["nice", "-n", "19", *command], capture_output=True, timeout=120,
                                              check=False))
            except (OSError, subprocess.SubprocessError) as error:
                problems.append(f"PDF figure conversion failed: {type(error).__name__}")
                return None
            if result.returncode != 0 or not output.is_file():
                problems.append("PDF figure conversion failed")
                return None
            data = output.read_bytes()
            if len(data) > MAX_VECTOR_FIGURE_BYTES:
                # A plot with very many points: its SVG is far larger than a sharp bitmap.
                bitmap = Path(temporary) / "figure"
                command = ["pdftocairo", "-png", "-r", "200", "-singlefile", "-f", "1", "-l", "1", str(path),
                           str(bitmap)]
                with _quiet():
                    result = (raw.run_tool(command, Path(temporary)) if raw.run_tool is not None
                              else subprocess.run(["nice", "-n", "19", *command], capture_output=True, timeout=120,
                                                  check=False))
                    png = bitmap.with_suffix(".png")
                    if result.returncode == 0 and png.is_file() and png.stat().st_size < len(data):
                        return files.asset(png.read_bytes(), ".png")
        try:
            _validate_svg(data, "figure")
        except EditionInvalid:
            problems.append("unsafe SVG")
            return None
        return files.asset(data, ".svg")
    if extension in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return files.asset(path.read_bytes(), ".jpg" if extension == ".jpeg" else extension)
    if extension == ".svg":
        data = path.read_bytes()
        try:
            _validate_svg(data, "figure")
        except EditionInvalid:
            problems.append("unsafe SVG")
            return None
        return files.asset(data, ".svg")
    problems.append(f"unsupported {extension or 'file'}")
    return None


IMAGE_EXTENSIONS = ("", ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp")


def image_resolver(search: list[Path], boundary: list[Path], recursive: list[Path] = ()) -> Callable[[str], Path | None]:
    """Find an authored figure the way graphicx/kpathsea does, never outside ``boundary``.

    ``search`` directories are tried in order; then the ``recursive`` roots (like
    TEXINPUTS ``//`` entries) for a unique file whose path ends with the authored one.
    """
    roots = [root.resolve() for root in boundary]

    def inside(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == root or root in resolved.parents for root in roots)

    def resolve(source: str) -> Path | None:
        source = unquote(source).strip()
        if (not source or source.startswith(("/", "data:")) or ".." in PurePosixPath(source).parts
                or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", source)):
            return None
        for base in search:
            for extension in IMAGE_EXTENSIONS:
                candidate = base / (source + extension)
                if candidate.is_file() and not candidate.is_symlink() and inside(candidate):
                    return candidate.resolve()
        for root in recursive:
            for extension in IMAGE_EXTENSIONS[1:] + ("",):
                name = PurePosixPath(source + extension)
                matches = sorted(
                    (path for path in root.rglob(name.name)
                     if path.is_file() and not path.is_symlink() and inside(path)
                     and path.as_posix().endswith("/" + name.as_posix())),
                    key=lambda path: (len(path.parts), path.as_posix()),
                )
                if matches:
                    return matches[0].resolve()
        return None

    return resolve


# ------------------------------------------------------------------ validation

_ALLOWED_A_SCHEMES = re.compile(r"^(https?:|mailto:|thoughts-link:|#)", re.I)
_FORBIDDEN_TAGS = {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "form",
                   "input", "button", "textarea", "select", "portal", "audio", "video", "source", "track",
                   "noscript", "template", "foreignobject", "animate", "set", "animatemotion",
                   "animatetransform", "handler", "listener"}
_URL_ATTRIBUTES = {"src", "href", "xlink:href", "{http://www.w3.org/1999/xlink}href", "srcset", "poster",
                   "action", "formaction", "background", "data", "lowsrc", "dynsrc", "ping", "cite",
                   "longdesc", "manifest", "codebase", "archive", "usemap", "content"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _check_css(text: str, names: set[str], where: str) -> None:
    if re.search(r"@import|expression\s*\(|behavior\s*:|-moz-binding|javascript:", text, re.I):
        raise EditionInvalid(f"{where}: CSS may not import, script or bind")
    for match in re.finditer(r"url\(\s*(['\"]?)([^)'\"]*)\1\s*\)", text, re.I):
        target = match.group(2).strip()
        if not (target.startswith(("#", "data:image/")) or target in names):
            raise EditionInvalid(f"{where}: CSS refers outside the edition: {target[:80]}")


def _check_reference(element_tag: str, name: str, value: str, names: set[str], where: str) -> None:
    value = value.strip()
    lowered = value.lower()
    if name in {"href"} and element_tag == "a":
        if _ALLOWED_A_SCHEMES.match(value):
            return
        raise EditionInvalid(f"{where}: link to an unsupported target: {value[:80]}")
    if name == "content" and element_tag != "meta":
        return
    if name == "content":
        if "url" in lowered or "refresh" in lowered:
            raise EditionInvalid(f"{where}: meta refresh is not allowed")
        return
    if name == "cite":
        return
    if value.startswith("#") or value in names:
        return
    if lowered.startswith("data:image/") and not lowered.startswith("data:image/svg"):
        return
    raise EditionInvalid(f"{where}: <{element_tag} {name}> refers outside the edition: {value[:80]}")


def _check_tree(root, names: set[str], where: str) -> None:
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        tag = _local_name(element.tag)
        if tag in _FORBIDDEN_TAGS:
            raise EditionInvalid(f"{where}: <{tag}> is not allowed")
        if tag == "link" and not (element.get("rel") == "stylesheet" and element.get("href") in names):
            raise EditionInvalid(f"{where}: only the edition's own stylesheet may be linked")
        if tag == "meta" and element.get("http-equiv"):
            raise EditionInvalid(f"{where}: <meta http-equiv> is not allowed")
        if tag == "style":
            _check_css(element.text or "", names, where)
        for name, value in element.attrib.items():
            local = _local_name(name) if name.startswith("{") else name.lower()
            if local.startswith("on"):
                raise EditionInvalid(f"{where}: event handler attribute {local} is not allowed")
            if local == "style":
                _check_css(value, names, where)
            if local in _URL_ATTRIBUTES or name in _URL_ATTRIBUTES:
                if tag == "link" and local == "href":
                    continue
                _check_reference(tag, "href" if local == "href" and tag == "a" else local, value, names, where)
            if "javascript:" in value.lower().replace(" ", "") and local not in {"data-key", "alt", "title"}:
                raise EditionInvalid(f"{where}: script URL in {local}")


def _validate_svg(data: bytes, where: str, names: set[str] | None = None) -> None:
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True)
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as error:
        raise EditionInvalid(f"{where}: not a well-formed SVG: {error}") from error
    if b"<!ENTITY" in data:
        raise EditionInvalid(f"{where}: SVG entities are not allowed")
    _check_tree(root, names or set(), where)


def validate(files: dict[str, bytes]) -> None:
    """Refuse an edition with scripts, network references, frames or files it does not hold."""
    if ENTRY not in files:
        raise EditionInvalid(f"the edition has no {ENTRY}")
    names = set(files)
    for name, data in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name != path.as_posix() or not name:
            raise EditionInvalid(f"unsafe file name in the edition: {name}")
        if name.endswith((".html", ".htm")):
            text = data.decode("utf-8")
            root = lxml.html.document_fromstring(text)
            _check_tree(root, names, name)
            if re.search(r"<\s*script", text, re.I):
                raise EditionInvalid(f"{name}: <script> is not allowed")
        elif name.endswith(".css"):
            _check_css(data.decode("utf-8", "replace"), names, name)
        elif name.endswith(".svg"):
            _validate_svg(data, name)
        elif not name.endswith((".png", ".jpg", ".gif", ".webp")):
            raise EditionInvalid(f"unsupported file type in the edition: {name}")


def media_type(name: str) -> str:
    return {
        ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    }[PurePosixPath(name).suffix.lower()]


def write_directory(edition: Edition, destination: Path) -> None:
    """Write the files (for inspection and tests); the server stores objects instead."""
    if destination.exists():
        shutil.rmtree(destination)
    for name, data in edition.files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
