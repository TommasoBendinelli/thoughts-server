# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A font TeX generates for itself inside the sandbox is not an undeclared build input."""

from __future__ import annotations

from pathlib import Path

from thoughts_server import build_sandbox, native_renderer


def test_a_bitmap_font_under_the_sandbox_texmfvar_is_accepted(monkeypatch):
    monkeypatch.delenv(build_sandbox.FONT_CACHE_ENVIRONMENT, raising=False)
    generated = Path(build_sandbox.SANDBOX_TEXMFVAR) / "fonts/pk/ljfour/public/bbm/bbm12.600pk"
    assert native_renderer._generated_tex_font(generated)
    assert native_renderer._generated_tex_font(Path(build_sandbox.SANDBOX_TEXMFVAR) / "fonts/tfm/public/bbm/bbm12.tfm")


def test_nothing_else_in_that_folder_becomes_an_accepted_input(monkeypatch):
    monkeypatch.delenv(build_sandbox.FONT_CACHE_ENVIRONMENT, raising=False)
    root = Path(build_sandbox.SANDBOX_TEXMFVAR)
    # A document could leave any file in its Library's TEXMFVAR: only generated fonts count.
    assert not native_renderer._generated_tex_font(root / "notes.tex")
    assert not native_renderer._generated_tex_font(root / "tex/latex/local/secret.sty")
    assert not native_renderer._generated_tex_font(Path("/home/tex/elsewhere/fonts/pk/x.600pk"))
    assert not native_renderer._generated_tex_font(Path("/tmp/fonts/pk/x.600pk"))


def test_the_library_font_cache_counts_when_the_build_runs_outside_the_sandbox(tmp_path, monkeypatch):
    cache = tmp_path / "state" / build_sandbox.FONT_CACHE_FOLDER
    (cache / "fonts/pk").mkdir(parents=True)
    monkeypatch.setenv(build_sandbox.FONT_CACHE_ENVIRONMENT, str(cache))
    assert native_renderer._generated_tex_font(cache.resolve() / "fonts/pk/bbm12.600pk")
    assert not native_renderer._generated_tex_font(cache.resolve() / "other.tex")
    assert not native_renderer._generated_tex_font(tmp_path / "state" / "fonts/pk/bbm12.600pk")
