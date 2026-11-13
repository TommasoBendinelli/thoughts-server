# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Run each independent project in a separate interpreter and stream its trace."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

from .child_python import SAFE_PATH_ENVIRONMENT
from .render_types import DocumentRenderError, RenderError


def render_project(docs_root: Path, publication_root: Path, artifact_root: Path, **options) -> list[Path]:
    request = {
        "docs_root": str(docs_root), "publication_root": str(publication_root),
        "artifact_root": str(artifact_root), **options,
    }
    request["_bootstrap_link_projects"] = sorted(request.get("_bootstrap_link_projects") or [])
    # Private control files are removed after the child has exited, including
    # when it fails. The coordinator alone promotes verified project outputs.
    with tempfile.TemporaryDirectory(prefix=".renderer-control.", dir=artifact_root.parent) as directory:
        control = Path(directory)
        request_file, response_file = control / "request.json", control / "response.json"
        request_file.write_text(json.dumps(request))
        environment = os.environ.copy()
        # Use the same package revision, including isolated worktree/test runs.
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(Path(__file__).resolve().parent.parent)]
            + [str(Path(entry).resolve()) for entry in environment.get("PYTHONPATH", "").split(os.pathsep)
               if entry]
        )
        # The document workspace is data, not an alternative Python package.
        environment.update(SAFE_PATH_ENVIRONMENT)
        command = [sys.executable, "-P", "-u", "-m", __name__, str(request_file), str(response_file)]
        # Keep the parent's process group so build timeout/cancellation kills
        # the project and its compiler descendants through the existing worker.
        with subprocess.Popen(command, cwd=docs_root, env=environment, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1) as process:
            try:
                for line in process.stdout:
                    print(line, end="", flush=True)
                code = process.wait()
            except BaseException:
                process.kill()
                process.wait()
                raise
        if not response_file.is_file():
            raise RenderError(f"Project renderer exited {code} without a result")
        response = json.loads(response_file.read_text())
        failure = response.get("failure")
        if failure:
            raise DocumentRenderError(**failure)
        if code != 0 or not isinstance(response.get("outputs"), list):
            raise RenderError(f"Project renderer returned an invalid result (exit {code})")
        expected = {publication_root / filename for filename in options["targets"]}
        outputs = [Path(path) for path in response["outputs"]]
        if set(outputs) != expected or len(outputs) != len(expected) or not all(p.is_file() for p in outputs):
            raise RenderError("Project renderer did not return its verified project PDF")
        return outputs


def main() -> int:
    from .native_renderer import render

    request_file, response_file = map(Path, sys.argv[1:])
    request = json.loads(request_file.read_text())
    for key in ("docs_root", "publication_root", "artifact_root"):
        request[key] = Path(request[key])
    request["_bootstrap_link_projects"] = set(request.get("_bootstrap_link_projects") or [])
    print(f"MJPDF_PROGRESS\tproject_process\t0\t1\tsteps\t"
          f"Rendering {request['targets'][0]} in process {os.getpid()}", flush=True)
    try:
        outputs = render(**request)
        response = {"outputs": [str(path) for path in outputs]}
        code = 0
    except DocumentRenderError as error:
        response = {"failure": {key: getattr(error, key) for key in
                    ("pdf_filename", "source", "code", "message")}}
        code = 1
    except Exception as error:
        traceback.print_exc()
        response = {"failure": {
            "pdf_filename": request["targets"][0],
            "source": request["targets"][0],
            "code": "project_build_failed", "message": str(error),
        }}
        code = 1
    response_file.write_text(json.dumps(response))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
