# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Figures a proposal adds (protocol/agent-proposals.md section 11), checked before anything is read.

A thoughts-cli extension of the desktop's requests: ``figures`` (a list of
``{"name", "svg"}``) on each entry of ``create-proposals`` and on the request of
``create-proposal`` and ``revise-proposal``. Each figure becomes the source
``<folder of the note>/agent-figures/<name>.svg``, published together with the
proposal, and its first line names the proposal that added it. The checks are the
server's (``thoughts_server.agent_figures``); a test keeps the two in step.
"""

from __future__ import annotations

import copy
import posixpath
import re
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

from .authoring import CREATE_BATCH, AuthoringRefused

FIGURE_DIRECTORY = "agent-figures"
FIGURE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_FIGURE_BYTES = 1024 * 1024
MAX_FIGURES = 20
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
FORBIDDEN_ELEMENTS = {"script", "foreignObject", "iframe", "object", "embed", "audio", "video"}
OWNER_MARK = re.compile(r'\A\s*<!-- thoughts-agent-figure proposal="([a-z0-9][a-z0-9._:-]{0,127})" -->')
XML_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>\s*")


@dataclass
class Figure:
    entry: int
    name: str
    svg: str


def refused(message: str) -> AuthoringRefused:
    return AuthoringRefused("invalid_figure", message)


def validate_svg(content: bytes) -> None:
    """The server's check: a self-contained SVG drawing, nothing that loads or runs anything."""
    if len(content) > MAX_FIGURE_BYTES:
        raise refused(f"A figure exceeds {MAX_FIGURE_BYTES // 1024} KiB.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise refused("A figure is not valid UTF-8.") from error
    if "\0" in text:
        raise refused("A figure contains a NUL byte.")
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise refused("A figure may not declare a DOCTYPE or entities.")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise refused(f"A figure is not well-formed XML: {error}") from error
    if root.tag not in {f"{{{SVG_NAMESPACE}}}svg", "svg"}:
        raise refused('A figure\'s root element must be <svg xmlns="http://www.w3.org/2000/svg">.')
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if local in FORBIDDEN_ELEMENTS:
            raise refused(f"A figure may not contain <{local}>.")
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1]
            if name.lower().startswith("on"):
                raise refused(f"A figure may not contain event handlers ({name}).")
            if attribute in {"href", XLINK_HREF}:
                target = value.strip()
                if not (target.startswith("#") or target.startswith("data:image/")):
                    raise refused("A figure may only link to its own elements or embed data:image/ URIs.")
        style = element.text if local == "style" else element.attrib.get("style", "")
        if style and re.search(r"url\(\s*['\"]?(?!#|data:image/)", style):
            raise refused("A figure's styles may only reference its own elements.")
        if style and "@import" in style:
            raise refused("A figure's styles may not import other stylesheets.")


def split_request(request: Any) -> tuple[Any, list[Figure]]:
    """The request without its figures (for the desktop's checks) and the figures it carried."""
    if not isinstance(request, dict):
        return request, []
    stripped = copy.deepcopy(request)
    figures: list[Figure] = []

    def take(holder: dict[str, Any], entry: int) -> None:
        if "figures" not in holder:
            return
        values = holder.pop("figures")
        if not isinstance(values, list):
            raise refused("figures must be a list of {name, svg} objects.")
        for value in values:
            if (not isinstance(value, dict) or set(value) != {"name", "svg"}
                    or not isinstance(value["name"], str) or not isinstance(value["svg"], str)):
                raise refused("Each figure takes exactly name and svg (both strings).")
            if FIGURE_NAME.fullmatch(value["name"]) is None:
                raise refused("A figure name is 1 to 64 characters a-z, 0-9, '-' or '_', starting with a letter "
                              "or digit.")
            svg = XML_DECLARATION.sub("", value["svg"], count=1)
            if OWNER_MARK.match(svg):
                raise refused("Leave out the thoughts-agent-figure comment; Thoughts writes it.")
            validate_svg(svg.encode("utf-8"))
            figures.append(Figure(entry, value["name"], svg if svg.endswith("\n") else svg + "\n"))

    if stripped.get("command") == CREATE_BATCH and isinstance(stripped.get("proposals"), list):
        for index, proposal in enumerate(stripped["proposals"]):
            if isinstance(proposal, dict):
                take(proposal, index)
    else:
        take(stripped, 0)
    if len(figures) > MAX_FIGURES:
        raise refused(f"A request may add at most {MAX_FIGURES} figures.")
    names = [figure.name for figure in figures]
    if len(set(names)) != len(names):
        raise refused("Two figures of the request have the same name.")
    return stripped, figures


def figure_path(source: str, name: str) -> str:
    return posixpath.join(posixpath.dirname(source), FIGURE_DIRECTORY, name + ".svg")


def figure_reference(source: str, name: str) -> str:
    """What the note writes: \\includegraphics{agent-figures/<name>} or ![...](agent-figures/<name>.svg)."""
    reference = f"{FIGURE_DIRECTORY}/{name}"
    return reference + ".svg" if source.lower().endswith((".md", ".markdown")) else reference


def references(replacement: str, name: str) -> bool:
    return re.search(re.escape(f"{FIGURE_DIRECTORY}/{name}") + r"(?![a-z0-9_-])", replacement) is not None


def source_tree_allows_figures(source: str) -> bool:
    parts = source.split("/")
    return any(parts[index] in {"tex", "markdown"} and parts[index + 1] == "source"
               for index in range(len(parts) - 2))


def owned_content(figure: Figure, proposal_id: str) -> bytes:
    return (f'<!-- thoughts-agent-figure proposal="{proposal_id}" -->\n' + figure.svg).encode("utf-8")


def owner_of(content: bytes) -> str | None:
    match = OWNER_MARK.match(content.decode("utf-8", errors="replace"))
    return match.group(1) if match else None
