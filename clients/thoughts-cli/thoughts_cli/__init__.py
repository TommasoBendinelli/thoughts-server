# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""thoughts-cli: a headless Thoughts sync client that creates, revises and withdraws agent proposals and adds new notes.

Library use (the claude.ai connector):

    from thoughts_cli import ThoughtsClient
    client = ThoughtsClient("http://127.0.0.1:8080", token)
    client.read_source("docs-x/tex/source/intro.tex")
    client.run({"command": "create-proposals", ...})   # thoughtsctl's request and result
    client.run({"command": "remove-proposal", ...})    # withdraw a pending agent proposal
    client.run({"command": "create-document", ...})    # a new note in a Library Project
    client.run({"command": "publish-build-inputs", ...})  # the build inputs of a note's PDF
    client.run({"command": "merge-review-cards", ...})  # merge review cards (the server does it)
    client.run({"command": "move-review-card", ...})    # onto a pending proposal: waits for its acceptance;
                                                        # start_line == end_line: a linked card, as a move pair
    client.run({"command": "pending-card-moves", ...})  # moves waiting for a proposal

Pure stdlib; Python 3.11 or later.
"""

__version__ = "1.7.0"

from .client import ThoughtsClient  # noqa: E402

__all__ = ["ThoughtsClient", "__version__"]
