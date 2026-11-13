# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""UTF-8 source positions must remain exact after removing prefix rescans."""

import pytest

from thoughts_server.renderer_tools.review_source_staging import _Utf8Offsets, instrument_source


@pytest.mark.parametrize(
    "text", ["", "plain ASCII\r\n", "é漢😀e\u0301\r\n", "\x00aé\x7f\u0080\u07ff\u0800\uffff\U0010ffff"]
)
def test_utf8_offsets_match_strict_python_boundaries(text):
    offsets = _Utf8Offsets(text)
    raw = text.encode("utf-8")
    boundaries = {len(text[:i].encode("utf-8")): i for i in range(len(text) + 1)}
    for byte, character in boundaries.items():
        assert offsets.byte_offset(character) == byte
        assert offsets.character_offset(byte) == character
    for byte in range(len(raw) + 1):
        if byte not in boundaries:
            with pytest.raises(UnicodeDecodeError):
                offsets.character_offset(byte)


def test_many_opaque_commands_keep_exact_source_spans():
    prefix = "\\begin{document}\nCaffè 漢 😀.\n"
    commands = [f"\\Opaque{{entry {i}: é漢😀}}" for i in range(1200)]
    raw = (prefix + "\n".join(commands) + "\n\\end{document}").encode()
    generated, report = instrument_source(raw)
    assert generated == raw
    assert len(report["uncovered"]) == len(commands)
    for record, command in zip(report["uncovered"], commands, strict=True):
        assert raw[record["source_start"] : record["source_end"]] == command.encode()
        assert record["reason"] == "opaque command Opaque"
