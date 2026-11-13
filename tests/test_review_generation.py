# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from dataclasses import replace

import pytest
from PIL import Image
from pydantic import ValidationError

from thoughts_server import review_generation
from thoughts_server.schemas import BuildRequest


def test_document_page_group_constrains_generation_discovery():
    schema = review_generation._discovery_schema(3, 12, {4, 5, 9})
    page = schema["properties"]["selections"]["items"]["properties"]["page"]
    assert page == {"type": "integer", "enum": [4, 5, 9]}


def test_review_generation_build_request_is_retired():
    with pytest.raises(ValidationError):
        BuildRequest.model_validate({
            "target": "paper.pdf",
            "repair_mode": "review_cards",
            "review_generation": {"expected_pdf_sha256": "a" * 64},
        })


def test_review_chatgpt_auth_ignores_api_key_and_uses_separate_login(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-key")
    api_settings = replace(settings, openai_api_key="configured-key")
    api = review_generation._codex_environment(api_settings)
    assert api["OPENAI_API_KEY"] == "configured-key"
    assert api["CODEX_HOME"] == str(settings.state_root / "codex")
    chatgpt = review_generation._codex_environment(
        replace(api_settings, review_agent_auth_method="chatgpt")
    )
    assert "OPENAI_API_KEY" not in chatgpt
    assert "CODEX_API_KEY" not in chatgpt
    assert chatgpt["CODEX_HOME"] == str(settings.state_root / "review-codex")
    with pytest.raises(ValueError, match="must be api or chatgpt"):
        review_generation._codex_environment(replace(api_settings, review_agent_auth_method="typo"))


def test_generate_review_candidates_uses_visual_geometry_and_filters_invalid_regions(
    settings,
    tmp_path,
    monkeypatch,
):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"immutable-pdf")
    calls = []

    def fake_codex(*args, images=None, **kwargs):
        supplied_images = images if images is not None else (args[6] if len(args) > 6 else None)
        calls.append((args, kwargs, supplied_images))
        if supplied_images is None:
            return {
                "schema_version": 3,
                "selections": [{"page": 2, "focus": "the main theorem"}],
            }
        return {
            "schema_version": 3,
            "candidates": [
                {
                    "page": 2,
                    "rect": [0.1, 0.2, 0.7, 0.6],
                    "question": "What does the theorem establish?",
                },
                {
                    "page": 2,
                    "rect": [0.0, 0.0, 1.0, 1.0],
                    "question": "This region is too large",
                },
            ],
        }

    def fake_render(_pdf_path, output_directory, pages, width):
        rendered = {}
        for page in pages:
            path = output_directory / f"page-{page}-{width}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (width, width // 2), "white").save(path)
            rendered[page] = path
        return rendered

    monkeypatch.setattr(review_generation, "_run_codex", fake_codex)
    monkeypatch.setattr(review_generation, "_render_pages", fake_render)
    progress = []

    candidates = review_generation.generate_review_candidates(
        replace(settings, docs_root=docs_root),
        pdf_path,
        "paper",
        "a" * 64,
        3,
        max_candidates=5,
        instruction="Create cards about the main claims.",
        model=None,
        reasoning_effort="high",
        avoid_regions=[{"page": 1, "rect": [0.1, 0.2, 0.4, 0.5]}],
        existing_cards=[
            {
                "card_id": "old-card",
                "question": "Existing question?",
                "answer_tex": [{"source_file": "other.tex", "tex": "Existing answer from another document."}],
                "answer_crop_paths": ["/srv/thoughts/example.png"],
            }
        ],
        progress=lambda *values: progress.append(values),
    )

    assert len(calls) == 2
    for args, _, _ in calls:
        assert "Existing question?" in args[3]
        assert "Existing answer from another document." in args[3]
        assert "Create cards about the main claims." in args[3]
        assert "Avoid overlapping these existing regions" not in args[3]
        assert "Use image inspection on those paths" in args[3]
    assert len(candidates) == 1
    assert candidates[0]["page"] == 2
    assert candidates[0]["question"] == "What does the theorem establish?"
    assert len(candidates[0]["page_visual_sha256"]) == 64
    assert [item[0] for item in progress] == [
        "review_page_selection",
        "review_region_placement",
        "review_candidates_ready",
    ]


def test_runner_preserves_complete_prompt_and_reports_error_not_echo(settings, tmp_path, monkeypatch):
    import json
    import subprocess

    monkeypatch.setattr(review_generation.shutil, "which", lambda *args, **kwargs: "/fake/codex")
    prompt = "Existing cards: " + "private answer text " * 1000
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            json.dumps({"type": "turn.failed", "error": {"message": "Selected model is unavailable"}}),
            "header\nuser\n" + prompt,
        )

    monkeypatch.setattr(review_generation.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="Selected model is unavailable") as raised:
        review_generation._run_codex(settings, tmp_path, {}, prompt, "test-model", "high")
    assert "private answer" not in str(raised.value)
    folder = next((settings.state_root / "review-agent-runs").iterdir())
    assert (folder / "prompt.txt").read_text() == prompt
    assert prompt in (folder / "stderr.txt").read_text()
    assert (folder / "prompt.txt").stat().st_mode & 0o777 == 0o600
    assert "--json" in calls[0]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_source_ranges_are_extracted_and_only_invalid_selections_retried(
    settings, tmp_path, monkeypatch, repair_succeeds,
):
    source = "Repeated answer.\n\\[\nw_t=\n\\prod_j a_j\n\\]\nRepeated answer.\n"
    path = "docs-paper/tex/source/main.tex"
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    calls = []

    def fake_codex(*args):
        calls.append(args)
        if len(calls) == 1:
            assert "whole sections" in args[3]
            return {"selections": [{"page": 1, "focus": "A whole section"}]}
        if len(calls) == 2:
            assert '"line": 3, "text": "w_t=\\n"' in args[3]
            segment = args[2]["properties"]["candidates"]["items"]["properties"]["source_segments"]["items"]
            assert set(segment["required"]) == {"source_file", "line_start", "line_end"}
            return {"candidates": [
                {"page": 1, "rect": [0.1, 0.1, 0.5, 0.3], "question": "What is the equation?",
                 "source_segments": [{"source_file": path, "line_start": 2, "line_end": 5}]},
                {"page": 1, "rect": [0.1, 0.4, 0.5, 0.6], "question": "What is repeated?",
                 "source_segments": [{"source_file": path, "line_start": 99, "line_end": 99}]},
            ]}
        assert '"index": 1' in args[3] and '"index": 0' not in args[3]
        return {"repairs": [{"index": 1, "source_segments": [
            {"source_file": path, "line_start": 6 if repair_succeeds else 99,
             "line_end": 6 if repair_succeeds else 99},
        ]}]}

    def fake_render(_pdf, output, pages, width):
        output.mkdir(parents=True)
        result = {}
        for page in pages:
            result[page] = output / f"{page}.png"
            Image.new("RGB", (20, 20), "white").save(result[page])
        return result

    monkeypatch.setattr(review_generation, "_run_codex", fake_codex)
    monkeypatch.setattr(review_generation, "_render_pages", fake_render)
    warnings = []
    result = review_generation.generate_review_candidates(
        replace(settings, docs_root=tmp_path), pdf, "paper", "a" * 64, 2,
        max_candidates=2, instruction="Review the section", model=None, reasoning_effort=None,
        avoid_regions=[], source_files={path: source}, warnings=warnings,
    )
    assert len(calls) == 3
    assert result[0]["source_segments"][0]["text"] == "\\[\nw_t=\n\\prod_j a_j\n\\]\n"
    assert len(result) == (2 if repair_succeeds else 1)
    if repair_succeeds:
        assert result[1]["source_segments"][0]["text"] == "Repeated answer.\n"
        assert not warnings
    else:
        assert len(warnings) == 1 and warnings[0]["question"] == "What is repeated?"


@pytest.mark.parametrize("auth_method", ["api", "chatgpt"])
def test_runner_attaches_images_individually_and_keeps_them(settings, tmp_path, monkeypatch, auth_method):
    import json
    import subprocess
    from pathlib import Path

    settings = replace(settings, review_agent_auth_method=auth_method)
    monkeypatch.setattr(review_generation.shutil, "which", lambda *args, **kwargs: "/fake/codex")
    images = []
    for index in range(2):
        path = tmp_path / f"page-{index}.png"
        Image.new("RGB", (10, 10)).save(path)
        images.append(path)

    def succeed(command, **kwargs):
        assert ('forced_login_method="chatgpt"' in command) == (auth_method == "chatgpt")
        assert command.count("--image") == 2
        for index, item in enumerate(command):
            if item == "--image":
                assert Path(command[index + 1]).read_bytes() == images[0].read_bytes()
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps({"selections": []}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(review_generation.subprocess, "run", succeed)
    assert review_generation._run_codex(settings, tmp_path, {}, "prompt", None, None, images) == {
        "selections": []
    }
