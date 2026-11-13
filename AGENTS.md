<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Thoughts server development

If `AGENTS.local.md` exists at the repository root, read it too: it holds the maintainer's machine-specific instructions.

## Setup

- Python 3.11 or later. Create the virtual environment with [uv](https://docs.astral.sh/uv/):

  ```bash
  uv venv .venv
  uv pip install --python .venv/bin/python -e '.[dev]'
  ```

  (`python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'` works too.)
- Markdown rendering needs the `markdown-renderer` extra, WeasyPrint's system libraries and, for formulas, Node.js with `npm ci --prefix src/thoughts_server/markdown_math`. LaTeX builds need TeX Live (`latexmk`, `pdflatex`, `bibtex`). Without them the tests that render Markdown or LaTeX fail or are skipped; compare such failures with the base revision before blaming a change.

## Tests

- Server: `PYTHONPATH=src .venv/bin/python -m pytest tests`.
- thoughts-cli (standard library only): `PYTHONPATH=src .venv/bin/python -m pytest clients/thoughts-cli/tests`; `tests/test_thoughts_cli*.py` run the client against this server in-process.
- HTML viewer: `node clients/html-viewer/tests/geometry.test.mjs`; with Playwright installed, `PLAYWRIGHT_CORE=<node_modules>/playwright-core node clients/html-viewer/tests/run.mjs --browser chromium`.
- Kotlin SDK: `gradle test` in `sdk/kotlin` (JDK and Gradle; the tests read `protocol/fixtures`).
- C++ SDK: `cmake -S sdk/cpp -B sdk/cpp/build && cmake --build sdk/cpp/build && ctest --test-dir sdk/cpp/build` (Qt 6.6 or later).
- Agent-proposal parsers across clients: `tools/proposal-conformance/run_all.sh`.
- Optional: `THOUGHTS_TEST_COLLOQUIUM_DECKS=<course repository>/slides` also runs the course-deck test of `tests/test_colloquium_slides.py`.
- Integration tests against a running server use a separate test instance (`deploy/fedora/TESTING.md`), never a production server: their own account, database, file roots, tokens and listener. Never upload synthetic test fixtures into a production database. Reset the test instance before and after each run, also when the run fails, check that it is empty afterwards, and keep the test report.

## Commit discipline

- Commit completed, coherent changes regularly and before handing off a finished coding task.
- During longer work, create clearly labeled checkpoint commits at useful milestones rather than letting changes accumulate across tasks.
- Before committing, inspect the diff, preserve unrelated work, and exclude credentials, local configuration, signing material, and generated build outputs.
- Run checks appropriate to the change and record any incomplete validation in the commit or handoff.
- Report the commit hash, whether it was pushed, and whether the running application or server uses that code as separate facts.
