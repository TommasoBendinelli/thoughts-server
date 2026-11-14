# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""MathJax SVGs must be well-formed XML before WeasyPrint reads them.

MathJax serializes its output as HTML, which leaves "<" bare inside attribute
values (its data-latex attribute holds the formula). A formula such as
x_{<t} then made every Markdown build of the document fail with
"not well-formed (invalid token)".
"""
import xml.etree.ElementTree as ET

from thoughts_server.markdown_renderer import _xml_attribute_safe

MATHJAX_LIKE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10ex" height="2ex" viewBox="0 -750 4000 1000">'
    '<g stroke="currentColor" fill="currentColor" data-mml-node="math" data-latex="f(y_{<k}) = a < b">'
    '<path d="M10 20L30 40"/></g></svg>'
)


def test_bare_less_than_in_an_attribute_is_escaped():
    try:
        ET.fromstring(MATHJAX_LIKE)
    except ET.ParseError:
        pass
    else:  # pragma: no cover - the fixture must reproduce the problem
        raise AssertionError("the fixture should not parse as XML before the fix")
    root = ET.fromstring(_xml_attribute_safe(MATHJAX_LIKE))
    group = root.find("{http://www.w3.org/2000/svg}g")
    assert group.attrib["data-latex"] == "f(y_{<k}) = a < b"


def test_svg_without_less_than_is_unchanged():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><g data-latex="x_t"><path d="M1 2"/></g></svg>'
    assert _xml_attribute_safe(svg) == svg
