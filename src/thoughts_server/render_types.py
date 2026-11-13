# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Shared value types and failures for the native render stages."""

from dataclasses import dataclass
from pathlib import Path


class RenderError(RuntimeError):
    """Raised when a native render cannot be validated and published."""


class CommandError(RenderError):
    """Raised when a renderer subprocess fails, retaining its captured output."""

    def __init__(self, return_code: int, command: list[str], output: str):
        self.return_code = return_code
        self.command = command
        self.output = output
        super().__init__(f"command failed with exit {return_code}: {' '.join(command)}")


class DocumentRenderError(RenderError):
    """A renderer failure attributed to one manifest-listed source document."""

    def __init__(self, pdf_filename: str, source: str, code: str, message: str):
        self.pdf_filename = pdf_filename
        self.source = source
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SourceBuild:
    project: str
    project_short: str
    tex_name: str
    source_id: str
    source_tex: Path
    pdf: Path
    synctex: Path | None
    fls: Path | None
    compile_cache_hit: bool = False
    source_format: str = "latex"
    renderer_manifest: Path | None = None
    title: str | None = None
    working_directory: str = "."


@dataclass(frozen=True)
class ProjectBuild:
    project: str
    project_short: str
    sources: tuple[SourceBuild, ...]

