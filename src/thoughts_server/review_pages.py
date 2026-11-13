# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rendered PDF pages of review cards and the visual identity clients compare."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image


def page_visual_sha256(image_path: Path) -> str:
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        height = max(1, round(rgb.height * 1024 / rgb.width))
        normalized = rgb.resize((1024, height), Image.Resampling.LANCZOS)
        digest = hashlib.sha256()
        digest.update(normalized.width.to_bytes(4, "big"))
        digest.update(normalized.height.to_bytes(4, "big"))
        digest.update(normalized.tobytes())
        return digest.hexdigest()


def render_pages(
    pdf_path: Path,
    output_directory: Path,
    pages: list[int],
    width: int,
) -> dict[int, Path]:
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        raise RuntimeError("pdftoppm is required to render review-card pages")
    output_directory.mkdir(parents=True, exist_ok=True)
    rendered: dict[int, Path] = {}
    for page in pages:
        prefix = output_directory / f"page-{page:04d}"
        completed = subprocess.run(
            [
                renderer,
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-scale-to-x",
                str(width),
                "-scale-to-y",
                "-1",
                "-png",
                str(pdf_path),
                str(prefix),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        path = prefix.with_suffix(".png")
        if completed.returncode != 0 or not path.is_file():
            raise RuntimeError(completed.stderr.strip() or f"could not render PDF page {page}")
        rendered[page] = path
    return rendered
