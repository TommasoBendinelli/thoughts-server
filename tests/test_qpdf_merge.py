# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from thoughts_server import native_renderer


@pytest.mark.parametrize("merge_code,check_code", [(0, None), (3, 0), (3, 2), (3, 3), (2, None)])
def test_merge_warning_requires_clean_validation(monkeypatch, capsys, merge_code, check_code):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if "--check" in command:
            return CompletedProcess(command, check_code, "output validation diagnostic\n")
        return CompletedProcess(command, merge_code, "input PDF warning\n")

    monkeypatch.setattr(native_renderer.subprocess, "run", run)
    monkeypatch.setattr(native_renderer.shutil, "which", lambda name: name)
    if merge_code == 2:
        with pytest.raises(native_renderer.CommandError) as error:
            native_renderer._merge_pdfs([Path("input.pdf")], Path("output.pdf"))
        assert error.value.return_code == 2
    elif check_code in (2, 3):
        with pytest.raises(
            native_renderer.RenderError, match="failed validation.*output validation diagnostic"
        ):
            native_renderer._merge_pdfs([Path("input.pdf")], Path("output.pdf"))
    else:
        native_renderer._merge_pdfs([Path("input.pdf")], Path("output.pdf"))

    output = capsys.readouterr().out
    assert len(calls) == (2 if merge_code == 3 else 1)
    if merge_code == 3:
        assert calls[1] == ["qpdf", "--check", "output.pdf"]
        assert "input PDF warning" in output
        assert ("passed validation; continuing" in output) == (check_code == 0)


def test_other_commands_still_reject_code_three(monkeypatch):
    monkeypatch.setattr(
        native_renderer.subprocess, "run",
        lambda command, **kwargs: CompletedProcess(command, 3, "failed\n"),
    )
    with pytest.raises(native_renderer.CommandError):
        native_renderer._run(["other-command"])
