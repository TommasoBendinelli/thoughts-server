# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Start the server's own Python programs next to a Library's files.

Build and preview workspaces hold files a client uploaded. Those files are
data. Python normally puts the working directory first on its module search
path for ``-m`` and ``-c``, so a client file named ``thoughts_server.py`` or
``json.py`` in a workspace root would otherwise run as the server. Every child
interpreter that may run in such a directory is therefore started with ``-P``,
and its environment carries ``PYTHONSAFEPATH`` so that an interpreter one of its
own children starts without the flag is covered too.
"""
from __future__ import annotations

import sys

SAFE_PATH_ENVIRONMENT = {"PYTHONSAFEPATH": "1"}


def module_command(module: str, *arguments: str) -> list[str]:
    """The command that runs ``python -m module`` without importing from the working directory."""
    return [sys.executable, "-P", "-m", module, *arguments]
