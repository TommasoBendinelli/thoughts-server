# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Versioned Markdown source identities, independent of PDF layout.

Pandoc decides whether an HTML comment is syntax or literal code. Its source
positions use Unicode columns (tabs expand to four columns), not UTF-8 bytes.
Only the conversion at this boundary may interpret those positions.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from dataclasses import dataclass

from .render_types import RenderError

PROFILE = "thoughts-commonmark-v1"
READER = "commonmark_x+sourcepos+tex_math_dollars"
PANDOC_VERSION = "3.9"
PREFIX = "thoughts-md-fragment-"
_KEY = r"(?:comment:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+|review:[A-Za-z0-9_-]+:(?:question|answer):[A-Za-z0-9_-]+)"
_MARKER = re.compile(r"<!--\s*thoughts:v1:(start|end|point)\s+(" + _KEY + r")\s*-->")
_POSITION = re.compile(r"(\d+):(\d+)-(\d+):(\d+)")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MarkdownError(RenderError):
    """Invalid source, unavailable renderer, or unverified placement."""


class SourcePositions:
    def __init__(self, raw: bytes):
        self.raw = raw
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MarkdownError("Markdown source must be UTF-8") from exc
        if "\x00" in text or text.startswith("\ufeff"):
            raise MarkdownError("NUL and UTF-8 BOM are not supported by this source profile")
        self.lines = []
        offset = 0
        for line in text.splitlines(keepends=True):
            # splitlines also recognizes Unicode separators that Pandoc does not.
            if line.rstrip("\r\n").endswith(("\v", "\f", "\x85", "\u2028", "\u2029")):
                raise MarkdownError("Unsupported source line separator")
            columns = {1: offset}
            column = 1
            for character in line.rstrip("\r\n"):
                column += 4 - (column - 1) % 4 if character == "\t" else 1
                offset += len(character.encode("utf-8"))
                columns[column] = offset
            offset += len(line) - len(line.rstrip("\r\n"))
            self.lines.append(columns)
        # CommonMark has a virtual newline even when the file has none.
        self.lines.append({1: len(raw)})

    def offset(self, line: int, column: int) -> int:
        try:
            if line < 1 or column < 1:
                raise KeyError
            return self.lines[line - 1][column]
        except (IndexError, KeyError) as exc:
            raise MarkdownError(f"Invalid Pandoc source position {line}:{column}") from exc

    def ranges(self, value: str) -> list[dict]:
        result = []
        for part in value.split(";"):
            match = _POSITION.fullmatch(part)
            if not match:
                raise MarkdownError(f"Missing or invalid source position: {value!r}")
            a, b, c, d = map(int, match.groups())
            start, end = self.offset(a, b), self.offset(c, d)
            if end <= start:
                raise MarkdownError("Empty or reversed source span")
            result.append({"byte_start": start, "byte_end": end, "sha256": digest(self.raw[start:end])})
        return result


@dataclass(frozen=True)
class ParsedMarkdown:
    ast: dict
    fragments: dict[str, dict]
    markers: dict[str, dict]
    metadata: dict[str, dict]


def _pandoc():
    try:
        import pypandoc

        version = str(pypandoc.get_pandoc_version())
    except (ImportError, OSError) as exc:
        raise MarkdownError("Install the markdown-renderer extra to render Markdown") from exc
    if version != PANDOC_VERSION:
        raise MarkdownError(f"Markdown source positions require Pandoc {PANDOC_VERSION}, got {version}")
    return pypandoc


def parse_markdown(raw: bytes, *, marker_anchors: bool = False) -> ParsedMarkdown:
    """Parse with source positions; markers never reach the rendered HTML.

    ``marker_anchors`` (HTML editions only) instead leaves an empty anchor element
    at each marker, with the canonical key of html_edition (never used for PDFs).
    """
    positions = SourcePositions(raw)
    pandoc = _pandoc()
    ast = json.loads(pandoc.convert_text(raw.decode("utf-8"), "json", format=READER))
    # Local image hyperlinks are expanded beside their label in the self-contained
    # reading PDF. Both the label and image remain bound to the original link's
    # source span; no generated Markdown or edited source bytes are needed.
    from urllib.parse import urlsplit
    from pathlib import PurePosixPath
    from .project_documents import MARKDOWN_ASSET_SUFFIXES

    def expand_image_links(value):
        if isinstance(value, list):
            return [expand_image_links(v) for v in value]
        if not isinstance(value, dict):
            return value
        if value.get('t') == 'Link':
            attr, label, target = value['c']
            url = urlsplit(target[0])
            if not url.scheme and not url.netloc and PurePosixPath(url.path).suffix.lower() in MARKDOWN_ASSET_SUFFIXES:
                image = {'t': 'Image', 'c': [copy.deepcopy(attr), [], copy.deepcopy(target)]}
                return {'t': 'Span', 'c': [['', [], []], [value, image]]}
        return {k: expand_image_links(v) for k, v in value.items()}

    ast['blocks'] = expand_image_links(ast['blocks'])
    fragments: dict[str, dict] = {}
    events = []
    metadata = {}
    source_labels: set[str] = set()

    def walk(value, position=""):
        if isinstance(value, list):
            return [walk(item, position) for item in value]
        if not isinstance(value, dict):
            return value
        kind, content = value.get("t"), value.get("c")
        if kind in {"Span", "Div", "Code", "CodeBlock", "Link", "Image", "Header"}:
            attr = content[1] if kind == "Header" else content[0]
            for key, val in attr[2]:
                if key == "data-pos":
                    position = val
            if attr[0].startswith(PREFIX):
                raise MarkdownError("Source IDs must not use the renderer's reserved prefix")
            if attr[0].startswith("source-label:"):
                label = attr[0][len("source-label:"):]
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", label):
                    raise MarkdownError(f"Invalid Source label: {attr[0]}")
                if label in source_labels:
                    raise MarkdownError(f"Duplicate Source label: {label}")
                source_labels.add(label)
        if kind in {"RawInline", "RawBlock"}:
            if content[0] != "html":
                raise MarkdownError("Raw non-HTML content is not supported")
            meta_match = re.fullmatch(r"<!--\s*thoughts:v1:meta\s+(\{.*\})\s*-->", content[1].strip(), re.S)
            if meta_match:
                try:
                    meta = json.loads(meta_match[1])
                    key = meta["key"]
                    if (
                        set(meta) != {"key", "contents", "revision"}
                        or not re.fullmatch(_KEY, key)
                        or not isinstance(meta["contents"], str)
                        or len(meta["contents"]) > 100_000
                        or type(meta["revision"]) is not int
                        or meta["revision"] < 0
                        or key in metadata
                    ):
                        raise ValueError("invalid metadata")
                except (KeyError, TypeError, ValueError) as exc:
                    raise MarkdownError("Invalid or duplicate Markdown comment metadata") from exc
                spans = positions.ranges(position)
                original = b"".join(raw[s["byte_start"] : s["byte_end"]] for s in spans)
                if original.decode().replace("\r\n", "\n").replace("\r", "\n").strip() != content[1].strip():
                    raise MarkdownError("Metadata source position does not match its original bytes")
                metadata[key] = {**meta, "source_ranges": spans}
                return {"t": kind, "c": ["html", ""]}
            match = _MARKER.fullmatch(content[1].strip())
            if match:
                spans = positions.ranges(position)
                original = b"".join(raw[s["byte_start"] : s["byte_end"]] for s in spans)
                if original.decode().replace("\r\n", "\n").replace("\r", "\n").strip() != content[1].strip():
                    raise MarkdownError("Marker source position does not match its original bytes")
                action, key = match.groups()
                if "--" in key:
                    raise MarkdownError("Marker IDs cannot contain an HTML comment delimiter")
                events.append((spans[0]["byte_start"], spans[-1]["byte_end"], action, key))
                if marker_anchors:
                    from .html_edition import canonical_markdown_key

                    canonical = canonical_markdown_key(key)
                    if canonical is not None:
                        anchor = (f'<span class="tm" data-tm="{action}" '
                                  f'data-key="{html.escape(canonical, quote=True)}"></span>')
                        return {"t": kind, "c": ["html", anchor]}
                return {"t": kind, "c": ["html", ""]}
            if re.fullmatch(r"<!-- thoughts:agent-proposal:v1:(?:start [A-Za-z0-9_-]+|end [a-z0-9][a-z0-9._:-]{0,127}) -->", content[1].strip()):
                return {"t": kind, "c": ["html", ""]}
            if re.fullmatch(r"<!-- thoughts:agent-proposal:v2:start\n(?:[^\n]*\n)*-->|<!-- thoughts:agent-proposal:v2:end [a-z0-9][a-z0-9._:-]{0,127} -->", content[1].strip()):
                return {"t": kind, "c": ["html", ""]}
            if "thoughts:" in content[1]:
                raise MarkdownError(
                    "Invalid/unsupported Thoughts marker, or block marker shares a prose line"
                )
            if not re.fullmatch(r"\s*<!--(?:(?!-->).)*-->\s*", content[1], flags=re.S):
                raise MarkdownError("Raw HTML is not supported by the first Markdown renderer profile")
            return copy.deepcopy(value)
        if kind in {"Str", "Code", "CodeBlock", "Math", "Image"}:
            identifier = f"{PREFIX}{len(fragments) + 1}"
            spans = positions.ranges(position)
            text = content if kind == "Str" else content[1] if kind in {"Code", "CodeBlock"} else ""
            fragments[identifier] = {"kind": kind, "text": text, "source_ranges": spans, "keys": []}
            node = copy.deepcopy(value)
            if kind in {"Image", "CodeBlock"}:
                fragments[identifier]["authored_id"] = node["c"][0][0]
                node["c"][0][0] = identifier
                return node
            if kind == "Math":
                fragments[identifier]["math"] = {
                    "tex": content[1],
                    "display": content[0]["t"] == "DisplayMath",
                }
            return {"t": "Span", "c": [[identifier, [], []], [node]]}
        return {key: walk(val, position) if key == "c" or kind is None else val for key, val in value.items()}

    # YAML metadata has no body source positions and must not become PDF text.
    # Keep its original bytes in the source snapshot, outside the placed body.
    instrumented = {**ast, "meta": {}, "blocks": walk(ast["blocks"])}
    markers: dict[str, dict] = {}
    active = {}
    for start, end, action, key in sorted(events):
        if action in {"start", "point"}:
            if key in markers:
                raise MarkdownError(f"Duplicate marker segment: {key}")
            markers[key] = {
                "kind": "point" if action == "point" else "range",
                "byte_start": end if action == "start" else start,
                "byte_end": end if action == "start" else start,
                "marker_start": start,
                "marker_end": end,
            }
            if action == "start":
                active[key] = end
        else:
            if key not in active:
                raise MarkdownError(f"Unmatched end marker: {key}")
            markers[key]["byte_end"] = start
            markers[key]["marker_end"] = end
            del active[key]
    if active:
        raise MarkdownError("Unclosed markers: " + ", ".join(sorted(active)))
    for key, marker in markers.items():
        if marker["kind"] == "point":
            continue
        matched = False
        for fragment in fragments.values():
            spans = fragment["source_ranges"]
            if all(
                marker["byte_start"] <= s["byte_start"] < s["byte_end"] <= marker["byte_end"] for s in spans
            ):
                fragment["keys"].append(key)
                matched = True
        if not matched:
            raise MarkdownError(f"Empty or unsupported marked passage: {key}; use a point marker")
    if set(metadata) - set(markers):
        raise MarkdownError("Markdown comment metadata has no matching source marker")
    return ParsedMarkdown(instrumented, fragments, markers, metadata)


def html_fragment(parsed: ParsedMarkdown, *, instrumented: bool, math_html: dict[str, str]) -> str:
    """Remove Pandoc's diagnostic wrappers; retain authored attributes and links."""

    def clean(value):
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict) and item.get("t") in {"Span", "Div"}:
                    attr, children = item["c"]
                    diagnostic = ["wrapper", "1"] in attr[2]
                    generated = attr[0] in parsed.fragments
                    if generated and parsed.fragments[attr[0]]["kind"] == "Math":
                        markup = math_html[attr[0]]
                        if not instrumented:
                            markup = markup.replace(f' id="{attr[0]}"', "")
                        result.append({"t": "RawInline", "c": ["html", markup]})
                        continue
                    if diagnostic or (generated and not instrumented):
                        result.extend(clean(children))
                        continue
                result.append(clean(item))
            return result
        if isinstance(value, dict):
            node = {key: clean(val) for key, val in value.items()}
            kind = node.get("t")
            if kind in {"Span", "Div", "Code", "CodeBlock", "Link", "Image", "Header"}:
                attr = node["c"][1] if kind == "Header" else node["c"][0]
                attr[2] = [pair for pair in attr[2] if pair[0] not in {"data-pos", "wrapper"}]
                if not instrumented and attr[0] in parsed.fragments:
                    attr[0] = parsed.fragments[attr[0]].get("authored_id", "")
            return node
        return value

    return _pandoc().convert_text(
        json.dumps(clean(parsed.ast)),
        "html5",
        format="json",
        extra_args=["--wrap=none", "--syntax-highlighting=none"],
    )


def mapped_html(parsed: ParsedMarkdown, math_html: dict[str, str]):
    """Unwrap source spans before shaping; retain character ownership out of band.

    Adding an inline span can break kerning/ligatures. Attributes on existing
    elements do not. Each existing element's own text and child tails therefore
    carry a parallel ownership sequence consumed by the layout adapter.
    """
    from xml.etree.ElementTree import Element, tostring

    import tinyhtml5

    raw_html = html_fragment(parsed, instrumented=True, math_html=math_html)
    original = tinyhtml5.parse(raw_html, namespace_html_elements=False)
    owners = {}
    text_maps = {}

    def append_text(parent, text, identifier):
        if not text:
            return
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + text
        else:
            parent.text = (parent.text or "") + text
        text_maps.setdefault(parent, []).extend((char, identifier) for char in text)

    def copy_element(old, parent, identifier=None):
        own_id = old.attrib.get("id")
        generated = own_id in parsed.fragments
        if generated:
            identifier = own_id
        if old.tag == "span" and generated:
            append_text(parent, old.text, identifier)
            for child in old:
                copy_element(child, parent, identifier)
                append_text(parent, child.tail, identifier)
            return
        if not isinstance(old.tag, str):
            return  # Ordinary HTML comments have no rendered content.
        element = Element(old.tag, {k: v for k, v in old.attrib.items() if k != "id" or not generated})
        if generated and parsed.fragments[own_id].get("authored_id"):
            element.set("id", parsed.fragments[own_id]["authored_id"])
        parent.append(element)
        if generated and parsed.fragments[own_id]["kind"] in {"Image", "Math"}:
            element.set("data-thoughts-object", own_id)
        append_text(element, old.text, identifier)
        for child in old:
            copy_element(child, element, identifier)
            append_text(element, child.tail, identifier)

    container = Element("container")
    copy_element(original, container)
    for element, mapping in text_maps.items():
        visible = [(char, identifier) for char, identifier in mapping if not char.isspace()]
        if visible:
            owner = f"node-{len(owners) + 1}"
            element.set("data-thoughts-text", owner)
            owners[owner] = visible
    actual = tostring(container[0], encoding="unicode", method="html")
    for element in container[0].iter():
        element.attrib.pop("data-thoughts-text", None)
        element.attrib.pop("data-thoughts-object", None)
    baseline = tostring(container[0], encoding="unicode", method="html")
    return actual, baseline, owners
