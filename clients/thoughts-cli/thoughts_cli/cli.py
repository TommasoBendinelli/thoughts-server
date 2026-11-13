# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""thoughts-cli: agent proposals, new notes and review cards as a headless Thoughts sync client.

    thoughts-cli read-source --source docs-x/tex/source/intro.tex
    echo '{"source": ..., "expected_source_sha256": ..., "start_line": 3, "end_line": 4,
           "replacement": "...", "explanation": "...", "conversation_id": ..., "request_id": ...}' \\
        | thoughts-cli create-proposal
    thoughts-cli create-document --project Ideas --name "Attention" --filename notes/attention.md \\
        --content-file attention.md --conversation-id ... --request-id ...
    thoughts-cli publish-build-inputs --document-id ... --publish --request-id ...
    thoughts-cli create-review-card --source ... --expected-source-sha256 ... --start-line 12 --end-line 15 \\
        --question "What is ...?" --due-at 2026-11-05T09:00:00+01:00 --request-id ...
    thoughts-cli move-review-card --card-id ... --source ... --expected-source-sha256 ... \\
        --start-line 12 --end-line 15 --request-id ...
    thoughts-cli merge-review-cards --card-id ... --card-id ... --source ... --expected-source-sha256 ... \\
        --request-id ...
    thoughts-cli pending-card-moves --card-id ...
    thoughts-cli cancel-pending-card-move --pending-move-id ...
    thoughts-cli add-html-page --folder docs-concepts-a6fe5d --name irt-middle-slope \\
        --html-file page.html --request-id ...
    thoughts-cli move-pdf --pdf Example_Paper_ebe6aaf90a9e --project MethodsComparison --request-id ...
    thoughts-cli build-status --document "SFT Memorizes, RL Generalizes (notes)"

Requests and results are thoughtsctl's (one JSON object on stdin without a
command field, one JSON result on stdout); the request fields can also be given
as options. Configuration: --server or THOUGHTS_SERVER_URL; the device token
from --token-file, THOUGHTS_TOKEN_FILE or THOUGHTS_TOKEN (never an option).
Exit status as thoughtsctl: 0 success, 2 invalid request, 3 not configured or
no answer (retry with the same request_id), 4 refused, 6 busy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .authoring import CREATE, CREATE_BATCH, REMOVE, REVISE
from .build_inputs import PUBLISH_BUILD_INPUTS
from .build_status import BUILD_STATUS
from .card_merges import MERGE_REVIEW_CARDS
from .card_pending_moves import CANCEL_PENDING_CARD_MOVE, PENDING_CARD_MOVES
from .client import ThoughtsClient, error_body
from .documents import CREATE_DOCUMENT, MAX_CONTENT_BYTES
from .html_pages import ADD_HTML_PAGE, MAX_PAGE_BYTES
from .pdf_moves import MOVE_PDF
from .review_cards import CREATE_REVIEW_CARD, MOVE_REVIEW_CARD

MAX_REQUEST_BYTES = 2 * 1024 * 1024
# A new document's content may take 4 MiB, more once escaped in JSON.
MAX_DOCUMENT_REQUEST_BYTES = 6 * MAX_CONTENT_BYTES + 64 * 1024
MAX_PAGE_REQUEST_BYTES = 6 * MAX_PAGE_BYTES + 64 * 1024
READ = "read-source"
COMMANDS = (READ, CREATE, CREATE_BATCH, REVISE, REMOVE, CREATE_DOCUMENT, PUBLISH_BUILD_INPUTS,
            CREATE_REVIEW_CARD, MOVE_REVIEW_CARD, MERGE_REVIEW_CARDS, ADD_HTML_PAGE, MOVE_PDF, BUILD_STATUS,
            PENDING_CARD_MOVES, CANCEL_PENDING_CARD_MOVE)


def exit_code(result: dict[str, Any]) -> int:
    """thoughtscommandprotocol syncCommandExitCode, with transport_error as 3."""
    code = result.get("error_code")
    if code == "busy":
        return 6
    if code in {"invalid_request", "unknown_operation"}:
        return 2
    if code in {"app_unavailable", "not_configured", "transport_unavailable", "transport_error"}:
        return 3
    if result.get("state") == "failed":
        return 4
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="thoughts-cli", description=__doc__.split("\n\n")[0],
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument("--version", action="version", version=f"thoughts-cli {__version__}")
    root.add_argument("--server", help="Server URL (default: THOUGHTS_SERVER_URL).")
    root.add_argument("--token-file", type=Path, help="File holding the device token (default: THOUGHTS_TOKEN_FILE, "
                                                      "or the token in THOUGHTS_TOKEN).")
    root.add_argument("--state-dir", type=Path, help="Request records (default: THOUGHTS_CLI_STATE or "
                                                     "~/.local/state/thoughts-cli).")
    root.add_argument("--cache-dir", type=Path, help="Source object cache for the proposal ID uniqueness check "
                                                     "(default: THOUGHTS_CLI_CACHE or ~/.cache/thoughts-cli).")
    root.add_argument("--no-uniqueness-scan", action="store_true",
                      help="Skip checking every other source for the proposal IDs (the desktop always checks).")
    root.add_argument("--no-render-check", action="store_true",
                      help="Do not ask the server to rebuild the PDFs after publishing.")
    root.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds (default 120).")
    root.add_argument("--pretty", action="store_true", help="Indent the JSON result.")
    root.add_argument("--json", action="store_true", help="Accepted for thoughtsctl compatibility; output is always JSON.")
    commands = root.add_subparsers(dest="command", required=True, metavar="COMMAND")

    read = commands.add_parser(READ, help="One source's exact text, source_sha256 and revision.")
    read.add_argument("--source")
    read.add_argument("--from-line", type=int)
    read.add_argument("--max-lines", type=int)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--source")
        sub.add_argument("--expected-source-sha256")
        sub.add_argument("--conversation-id")
        sub.add_argument("--request-id")

    def text_fields(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--replacement")
        sub.add_argument("--replacement-file", type=Path, help="Read the replacement from a file (exact bytes).")
        sub.add_argument("--explanation")
        sub.add_argument("--explanation-file", type=Path)

    single = commands.add_parser(CREATE, help="One addition, replacement or deletion.")
    common(single)
    text_fields(single)
    single.add_argument("--start-line", type=int)
    single.add_argument("--end-line", type=int)
    single.add_argument("--conversation-url")
    batch = commands.add_parser(CREATE_BATCH, help="Several proposals against one source version.")
    common(batch)
    batch.add_argument("--proposals-file", type=Path,
                       help="JSON array of {start_line, end_line, replacement, explanation}.")
    batch.add_argument("--conversation-url")
    revise = commands.add_parser(REVISE, help="New proposed text and explanation for a pending proposal.")
    common(revise)
    text_fields(revise)
    revise.add_argument("--proposal-id")
    remove = commands.add_parser(REMOVE, help="Withdraw a pending agent proposal and restore its original text.")
    common(remove)
    remove.add_argument("--proposal-id")
    remove.add_argument("--explanation")
    remove.add_argument("--explanation-file", type=Path)
    document = commands.add_parser(CREATE_DOCUMENT, help="A new Markdown or LaTeX note in a Library Project.")
    document.add_argument("--project", help="The Project's id or exact name.")
    document.add_argument("--name", help="The document's title in the Library.")
    document.add_argument("--filename", help="Path inside the Project's default folder (.md, .markdown or .tex).")
    document.add_argument("--content")
    document.add_argument("--content-file", type=Path, help="Read the content from a file (exact bytes, UTF-8).")
    document.add_argument("--conversation-id")
    document.add_argument("--request-id")
    inputs = commands.add_parser(PUBLISH_BUILD_INPUTS, help="Publish the build inputs of a note's PDF, as the Mac "
                                                           "does, so the server can build a note created elsewhere.")
    inputs.add_argument("--document-id", help="The document's id (as list_documents or create-document gives it).")
    inputs.add_argument("--project", help="Instead of --document-id: the Project's id or exact name ...")
    inputs.add_argument("--filename", help="... and the note's path inside the Project's default folder.")
    inputs.add_argument("--publish", action="store_true", default=None,
                        help="Publish the note (if unpublished) once its inputs are registered.")
    inputs.add_argument("--request-id")

    def selection(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--source")
        sub.add_argument("--expected-source-sha256")
        sub.add_argument("--start-line", type=int, help="One-based first complete line of the answer.")
        sub.add_argument("--end-line", type=int, help="Exclusive end line of the answer.")
        sub.add_argument("--request-id")

    card = commands.add_parser(CREATE_REVIEW_CARD, help="A new review card whose answer is a source passage, "
                                                       "marked in the source.")
    selection(card)
    card.add_argument("--question")
    card.add_argument("--due-at", help="First review time, ISO 8601 with a timezone.")
    moved = commands.add_parser(MOVE_REVIEW_CARD, help="Move a card without source markers (e.g. on an imported "
                                                      "PDF) into a note, keeping its history; onto a pending "
                                                      "proposal's text once the user accepts it.")
    moved.add_argument("--card-id")
    selection(moved)
    waiting = commands.add_parser(PENDING_CARD_MOVES, help="Card moves that wait for a pending proposal (reads only).")
    waiting.add_argument("--card-id")
    waiting.add_argument("--proposal-id")
    waiting.add_argument("--state", help="waiting_for_proposal (default), moved, dropped, cancelled or all.")
    cancelled = commands.add_parser(CANCEL_PENDING_CARD_MOVE, help="Cancel a card move that waits for a proposal; "
                                                                   "the card stays where it is.")
    cancelled.add_argument("--pending-move-id")
    merged = commands.add_parser(MERGE_REVIEW_CARDS, help="Merge 2 to 20 review cards of one document into one card "
                                                          "with every card's passage, keeping one card's history.")
    merged.add_argument("--card-id", dest="card_ids", action="append", help="A card to merge (give it 2 to 20 times).")
    merged.add_argument("--keep-card-id", help="The card that stays (default: the one with the most reviews).")
    merged.add_argument("--question", help="The merged card's question (default: the questions joined).")
    merged.add_argument("--source")
    merged.add_argument("--expected-source-sha256")
    merged.add_argument("--request-id")
    page = commands.add_parser(ADD_HTML_PAGE, help="Add or replace one self-contained HTML page of a docs folder "
                                                  "(docs-x/tex/support/html/<name>.html), for thoughts-html: links.")
    page.add_argument("--folder", help="The docs-* source folder.")
    page.add_argument("--name", help="The page's file name (.html is added).")
    page.add_argument("--html-file", type=Path, help="The page (exact bytes, UTF-8).")
    page.add_argument("--replace", action="store_true", default=None, help="Replace an existing page of that name.")
    page.add_argument("--request-id")
    move = commands.add_parser(MOVE_PDF, help="Move an imported PDF to another Library Project (with its review "
                                              "cards); links to it keep working.")
    move.add_argument("--pdf", help="The PDF's document id, Library filename or name.")
    move.add_argument("--project", help="The destination Project's id or exact name.")
    move.add_argument("--request-id")
    status = commands.add_parser(BUILD_STATUS, help="Why a document's PDF does not build, and the pending proposal "
                                                    "most likely to blame (reads only).")
    status.add_argument("--document", help="The document's id, name, PDF filename or source path.")
    return root


FIELD_OPTIONS = {
    "source": "source", "expected_source_sha256": "expected_source_sha256", "conversation_id": "conversation_id",
    "request_id": "request_id", "conversation_url": "conversation_url", "start_line": "start_line",
    "end_line": "end_line", "replacement": "replacement", "explanation": "explanation", "proposal_id": "proposal_id",
    "from_line": "from_line", "max_lines": "max_lines", "project": "project", "name": "name", "filename": "filename",
    "content": "content", "document_id": "document_id", "publish": "publish", "question": "question",
    "due_at": "due_at", "card_id": "card_id", "folder": "folder", "replace": "replace", "pdf": "pdf",
    "document": "document", "card_ids": "card_ids", "keep_card_id": "keep_card_id",
    "pending_move_id": "pending_move_id", "state": "state",
}
FILE_OPTIONS = {"replacement_file": "replacement", "explanation_file": "explanation", "proposals_file": "proposals",
                "content_file": "content", "html_file": "html"}


class UsageError(Exception):
    pass


def request_from_options(arguments: argparse.Namespace) -> dict[str, Any] | None:
    request: dict[str, Any] = {}
    for option, field in FIELD_OPTIONS.items():
        value = getattr(arguments, option, None)
        if value is not None:
            request[field] = value
    for option, field in FILE_OPTIONS.items():
        location = getattr(arguments, option, None)
        if location is None:
            continue
        if field in request:
            raise UsageError(f"give {field} either as text or as a file, not both")
        try:
            data = location.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise UsageError(f"cannot read {location}: {error}") from None
        if field == "proposals":
            try:
                request[field] = json.loads(data)
            except ValueError as error:
                raise UsageError(f"{location} is not JSON: {error}") from None
        else:
            request[field] = data
    return request or None


def request_from_stdin(limit: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
    refusal = f"Provide a JSON object of at most {limit // (1024 * 1024)} MiB without a command field."
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise UsageError(refusal)
    try:
        request = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UsageError(refusal) from None
    if not isinstance(request, dict) or "command" in request:
        raise UsageError(refusal)
    return request


def make_client(arguments: argparse.Namespace) -> ThoughtsClient:
    server = arguments.server or os.environ.get("THOUGHTS_SERVER_URL", "")
    token_file = arguments.token_file or (Path(os.environ["THOUGHTS_TOKEN_FILE"])
                                          if os.environ.get("THOUGHTS_TOKEN_FILE") else None)
    if token_file is not None:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise LookupError(f"cannot read the token file {token_file}: {error.strerror}") from None
    else:
        token = os.environ.get("THOUGHTS_TOKEN", "").strip()
    if not server or not token:
        raise LookupError("Configure the server (--server or THOUGHTS_SERVER_URL) and a device token "
                          "(--token-file, THOUGHTS_TOKEN_FILE or THOUGHTS_TOKEN).")
    state = arguments.state_dir or (Path(os.environ["THOUGHTS_CLI_STATE"]) if os.environ.get("THOUGHTS_CLI_STATE") else None)
    cache = arguments.cache_dir or (Path(os.environ["THOUGHTS_CLI_CACHE"]) if os.environ.get("THOUGHTS_CLI_CACHE") else None)
    return ThoughtsClient(server, token, state_dir=state, cache_dir=cache,
                          uniqueness_scan=not arguments.no_uniqueness_scan,
                          render_check=not arguments.no_render_check, timeout=arguments.timeout)


def run(argv: list[str] | None = None) -> tuple[dict[str, Any], int]:
    arguments = parser().parse_args(argv)
    try:
        request = request_from_options(arguments)
        if request is None:
            request = request_from_stdin(MAX_DOCUMENT_REQUEST_BYTES if arguments.command == CREATE_DOCUMENT
                                         else MAX_PAGE_REQUEST_BYTES if arguments.command == ADD_HTML_PAGE
                                         else MAX_REQUEST_BYTES)
    except UsageError as error:
        result = error_body("invalid_request", str(error))
        return result, exit_code(result)
    try:
        client = make_client(arguments)
    except LookupError as error:
        result = error_body("not_configured", str(error))
        return result, exit_code(result)
    if arguments.command == READ:
        unexpected = set(request) - {"source", "from_line", "max_lines"}
        if unexpected or not isinstance(request.get("source"), str):
            result = error_body("invalid_request", "read-source takes source and optionally from_line and max_lines.")
            return result, exit_code(result)
        try:
            result = client.read_source(request["source"], int(request.get("from_line") or 1),
                                        None if request.get("max_lines") is None else int(request["max_lines"]))
        except (TypeError, ValueError):
            result = error_body("invalid_request", "from_line and max_lines must be integers.")
    else:
        result = client.run({"command": arguments.command, **request})
    return result, exit_code(result)


def main(argv: list[str] | None = None) -> int:
    result, status = run(argv)
    pretty = "--pretty" in (sys.argv[1:] if argv is None else argv)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2 if pretty else None,
                                separators=None if pretty else (",", ":")) + "\n")
    sys.stdout.flush()
    return status
