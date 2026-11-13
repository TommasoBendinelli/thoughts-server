# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Isolated preview worker; the API owns its deadline, cache lock and publication."""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .proposal_previews import _render_preview


def main() -> int:
    request_path = Path(sys.argv[1])
    workspace = request_path.parent
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        settings = replace(
            Settings.from_env(),
            worktree_root=Path(request["worktree_root"]),
            database_url=request["database_url"],
            object_root=Path(request["object_root"]),
            build_parallel_jobs=request["build_parallel_jobs"],
        )
        result = _render_preview(
            settings,
            request["manifest"],
            Path(request["current_pdf"]),
            request["artifact_sha256"],
            request["proposal_id"],
            workspace,
        )
        (workspace / "result.json").write_text(json.dumps(result), encoding="utf-8")
        return 0
    except Exception as error:
        traceback.print_exc()
        (workspace / "error.json").write_text(json.dumps({"error": str(error)[:2000]}), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
