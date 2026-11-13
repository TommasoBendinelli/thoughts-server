#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Regenerate the fixtures, run every driveable implementation, and compare.
#   SERVER_SRC  thoughts-server src directory to test (default: this checkout's src)
#   PY          Python with the server dependencies (default: ~/repos/thoughts-server/.venv/bin/python;
#               PYTHONPATH below makes it import SERVER_SRC instead of the main checkout)
#   DESKTOP     desktop checkout (read only; default ~/repos/thoughts-desktop-integration), built into /tmp
#   ANDROID     Android checkout (read only; default ~/repos/thoughts-android-integration)
#   VSCODE      VS Code extension directory (read only)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
FIXTURES="$ROOT/protocol/fixtures/agent-proposals/cases.json"
SERVER_SRC=${SERVER_SRC:-$ROOT/src}
PY=${PY:-$HOME/repos/thoughts-server/.venv/bin/python}
mkdir -p "$HERE/results"
python3 "$ROOT/protocol/fixtures/agent-proposals/gen_cases.py"
python3 "$HERE/runners/reference_runner.py" "$FIXTURES" "$HERE/results/reference.json"
PYTHONPATH="$SERVER_SRC" "$PY" "$HERE/runners/python_server.py" "$FIXTURES" "$HERE/results/python.json"
BIN=$("$HERE/runners/build_desktop.sh")
"$BIN" "$FIXTURES" "$HERE/results/desktop.json"
node "$HERE/runners/js_runner.cjs" "$FIXTURES" "$HERE/results/js.json"
python3 "$HERE/compare.py" "$FIXTURES"
