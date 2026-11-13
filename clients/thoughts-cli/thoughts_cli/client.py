# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Proposal authoring as a sync client: read the source, write the proposals, publish.

The client reads the current source from the server, checks it against the
request's ``expected_source_sha256``, writes the proposals exactly as the
desktop does (authoring.py) and publishes the new source through
POST /v1/source-publications with the same payload as the Mac's source
publisher: one ``upsert`` mutation whose base is the version it read
(base_revision, base_sha256, base_object_sha256), the device's next operation
sequence, a fresh operation and publication ID, and the authored bytes uploaded
first to POST /v1/objects.

Concurrency (README.md, "Concurrent changes"): the source is read again just
before publishing and a change refuses the request with source_conflict, as a
stale expected_source_sha256 does. A change that lands in the remaining
milliseconds is handled by the server as for every device: it merges the two
changes three ways; overlapping changes are a conflict, and the whole
publication is refused (409, nothing published), which the client answers with
source_conflict. A clean merge ("merged") is verified: the proposals must be
present with the exact original and proposed text, and the result names the
merged version.

create-document (documents.py) adds a new note instead: it publishes a new
source (revision 0, the empty base) in a Project's default folder and declares
it with the Library-project actions include_source and rename_document, as the
desktop and Android do; it refuses a path that already exists. Right after the
declaration it registers the new PDF's build inputs (build_inputs.py), which Fedora
needs to build it and which otherwise only the Mac publishes; without them the note
is unpublished, since a published note without inputs fails every Library build.
publish-build-inputs does that for a note created earlier, and publishes it after.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (authoring, build_inputs, build_status, card_merges, card_pending_moves, documents, figures, guards,
               html_pages, pdf_links, pdf_moves, review_cards)
from .api import ApiError, ServerApi, TransportError, sha256_hex
from .proposal_syntax import parse, source_format
from .state import Busy, RequestStore, default_state_dir, token_fingerprint

EMPTY_SHA256 = sha256_hex(b"")
MAX_SEQUENCE_RECOVERIES = 3
MAXIMUM_DEVICE_SEQUENCE = 2_147_483_647
COMMANDS = (authoring.CREATE, authoring.CREATE_BATCH, authoring.REVISE, documents.CREATE_DOCUMENT,
            build_inputs.PUBLISH_BUILD_INPUTS, *review_cards.COMMANDS, card_merges.MERGE_REVIEW_CARDS, pdf_moves.MOVE_PDF,
            build_status.BUILD_STATUS, *card_pending_moves.COMMANDS)
# A Library-project action refused because another device changed the Project
# (thoughts_server.library_projects._mutate): read the catalogue again and resend.
PROJECT_CHANGED = "changed on another device"
MAX_PROJECT_ATTEMPTS = 3
MAX_SOURCE_PATH = 1024


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "thoughts-cli"


def client_timestamp() -> str:
    """The Mac publisher's client_timestamp: UTC, microseconds when nonzero, Z."""
    now = datetime.now(UTC)
    text = now.strftime("%Y-%m-%dT%H:%M:%S")
    if now.microsecond:
        text += f".{now.microsecond:06d}"
    return text + "Z"


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return authoring.AuthoringRefused(code, message, **extra).body()


class ObjectCache:
    """Source objects by SHA-256 on disk, so the uniqueness check downloads only what changed."""

    def __init__(self, directory: Path, api: ServerApi):
        self.directory = Path(directory) / "objects"
        self.api = api

    def get(self, digest: str) -> bytes:
        target = self.directory / digest
        try:
            data = target.read_bytes()
            if sha256_hex(data) == digest:
                return data
        except OSError:
            pass
        data = self.api.download(digest)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.directory / f".{digest}.tmp"
            temporary.write_bytes(data)
            os.replace(temporary, target)
        except OSError:
            pass
        return data

    def prune(self, keep: set[str]) -> None:
        try:
            for item in self.directory.iterdir():
                if item.name not in keep:
                    item.unlink(missing_ok=True)
        except OSError:
            pass


class ThoughtsClient:
    def __init__(self, server_url: str, token: str, *, state_dir: Path | None = None,
                 cache_dir: Path | None = None, uniqueness_scan: bool = True, render_check: bool = True,
                 timeout: float = 120.0, now: Callable[[], datetime] | None = None,
                 new_id: Callable[[], str] | None = None, api: ServerApi | None = None):
        if not server_url or not token:
            raise ValueError("a server URL and a device token are required")
        self.server_url = server_url.rstrip("/")
        self.api = api or ServerApi(self.server_url, token, timeout=timeout)
        self.store = RequestStore(state_dir or default_state_dir())
        self.cache = ObjectCache(cache_dir or default_cache_dir(), self.api)
        self.scope = f"{self.server_url}#{token_fingerprint(token)}"
        self.uniqueness_scan = uniqueness_scan
        self.render_check = render_check
        self.now = now or (lambda: datetime.now(UTC))
        self.new_id = new_id

    # ------------------------------------------------------------------ reading

    def read_source(self, path: str, from_line: int = 1, max_lines: int | None = None) -> dict[str, Any]:
        """The exact text of one source with its SHA-256 and revision: what a proposal request needs."""
        try:
            snapshot, raw = self._read(path)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)
        text = raw.decode("utf-8", "replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        first = max(1, int(from_line))
        last = len(lines) if max_lines is None else min(len(lines), first - 1 + max(1, int(max_lines)))
        pieces = text.split("\n")
        kept = [piece + ("\n" if index < len(pieces) - 1 else "")
                for index, piece in enumerate(pieces[first - 1:last], first - 1)]
        return {
            "schema_version": 1, "accepted": True, "terminal": True, "state": "source_read", "source": path,
            "source_sha256": snapshot["sha256"], "source_revision": snapshot["revision"],
            "line_count": len(lines), "from_line": first, "to_line": max(last, first - 1),
            "next_line": last + 1 if last < len(lines) else None, "text": "".join(kept),
        }

    def _read(self, path: str) -> tuple[dict[str, Any], bytes]:
        if not isinstance(path, str) or not path or "\0" in path or source_format(path) is None:
            raise authoring.AuthoringRefused(
                "invalid_source", "The source must be an existing .tex, .md, or .markdown file of this Library "
                "(its path as GET /v1/sources lists it).")
        snapshot = self.api.source(path)
        if snapshot is None or snapshot.get("deleted"):
            raise authoring.AuthoringRefused(
                "invalid_source", "The source must be an existing .tex, .md, or .markdown file of this Library "
                "(its path as GET /v1/sources lists it).")
        try:
            raw = self.api.download(snapshot["object_sha256"])
        except ApiError as error:
            raise authoring.AuthoringRefused("read_failed", f"The source object is unavailable: {error}") from error
        except TransportError as error:
            if "verification" in str(error):
                raise authoring.AuthoringRefused("read_failed", "The source object failed its identity check.") from error
            raise
        if sha256_hex(raw) != snapshot["sha256"]:
            raise authoring.AuthoringRefused("read_failed", "The source object failed its identity check.")
        return snapshot, raw

    # ------------------------------------------------------------------ authoring

    def run(self, request: Any) -> dict[str, Any]:
        """Create or revise proposals (a thoughtsctl request with its command) or create a document
        (create-document); never raises for a refusal."""
        if isinstance(request, dict) and request.get("command") == documents.CREATE_DOCUMENT:
            return self.create_document(request)
        if isinstance(request, dict) and request.get("command") == build_inputs.PUBLISH_BUILD_INPUTS:
            return self.publish_build_inputs(request)
        if isinstance(request, dict) and request.get("command") in review_cards.COMMANDS:
            return self.review_card(request)
        if isinstance(request, dict) and request.get("command") == card_merges.MERGE_REVIEW_CARDS:
            return self.merge_review_cards(request)
        if isinstance(request, dict) and request.get("command") in card_pending_moves.COMMANDS:
            return self.pending_card_moves(request)
        if isinstance(request, dict) and request.get("command") == html_pages.ADD_HTML_PAGE:
            return self.add_html_page(request)
        if isinstance(request, dict) and request.get("command") == pdf_moves.MOVE_PDF:
            return self.move_pdf(request)
        if isinstance(request, dict) and request.get("command") == build_status.BUILD_STATUS:
            return self.build_status(request)
        try:
            stripped, figure_list = figures.split_request(request)
            validated = authoring.validate_request(stripped)
            if figure_list and not figures.source_tree_allows_figures(validated.source):
                raise figures.refused("Figures can be added only to notes in a tex/source or markdown/source tree.")
            for figure in figure_list:
                if not figures.references(validated.entries[figure.entry].replacement, figure.name):
                    raise figures.refused(
                        f"The replacement must reference the figure {figure.name!r} as "
                        f"{figures.figure_reference(validated.source, figure.name)!r}.")
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        digest = authoring.request_sha256(request)
        key = f"{self.scope}#{validated.request_id}"
        try:
            with self.store.locked():
                record = self.store.get(key)
                if record is not None:
                    if record.get("request_sha256") != digest:
                        return error_body("request_conflict", "This request ID was already used with different arguments.")
                    if record.get("status") == "applied":
                        return {**record["result"], "replayed": True}
                    # Prepared: the answer to its publication was lost; send the same one again.
                    return self._publish(key, record)
                return self._author(key, digest, validated, figure_list)
        except Busy as busy:
            return error_body("busy", str(busy))
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    def _author(self, key: str, digest: str, validated: authoring.AuthoringRequest,
                figure_list: list[figures.Figure] = ()) -> dict[str, Any]:
        path = validated.source
        snapshot, raw = self._read(path)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is None or "\0" in text:
            raise authoring.AuthoringRefused("invalid_source", "Proposal authoring requires valid UTF-8 source without NUL.")
        # Generated files are rewritten by their generator; a withdrawal may still clean one up (8).
        if not validated.remove and authoring.is_generated_source(authoring.owner_relative(path), text):
            raise authoring.AuthoringRefused(
                "generated_source",
                "This file is generated; change its generator or the generator's inputs instead of proposing an edit here.")
        if snapshot["sha256"] != validated.expected_source_sha256:
            raise authoring.AuthoringRefused(
                "source_conflict", "The source changed; read it again before preparing this edit.",
                current_source_sha256=snapshot["sha256"])
        options: dict[str, Any] = {"now": self.now(), "previous_sha256": snapshot["sha256"]}
        if self.new_id is not None:
            options["new_id"] = self.new_id
        if self.uniqueness_scan and not validated.remove:
            options["ids_elsewhere"] = self._ids_elsewhere(path)
        guard_warnings: list[dict[str, Any]] = []
        if not validated.remove:
            # Preventable build failures (guards.py): figure paths are rewritten, misplaced proposals refused.
            for index, entry in enumerate(validated.entries):
                entry.replacement, rewritten = guards.rewrite_include_paths(
                    path, entry.replacement, proposal_index=index if validated.batch else None)
                guard_warnings += rewritten
            if validated.create:
                offsets = guards.line_offsets(text)
                last = len(offsets)
                guards.check_placement(path, text, [
                    (offsets[min(entry.start_line, last) - 1], offsets[min(entry.end_line, last) - 1], index)
                    for index, entry in enumerate(validated.entries)])
        authored = authoring.author(validated, path, text, **options)
        if not validated.remove:
            by_id = parse(path, authored.updated).by_id()
            written = []
            for index, entry in enumerate(validated.entries):
                guards.check_markdown(path, entry.replacement, entry.before,
                                      proposal_index=index if validated.batch else None)
                if (found := by_id.get(entry.id)) is not None:
                    written.append((found.start, found.end, entry.replacement, index))
            guards.check_card_masks(path, authored.updated, written)
            # A thoughts-html: link the edit adds must name a page of the note's folder,
            # and a Library PDF link must name a PDF some Project selects (pdf_links.py).
            html_pages.check_links(self.api, path, text, authored.updated)
            guard_warnings += pdf_links.check(self.api.library_projects, path, text, authored.updated)
            if guard_warnings:
                authored.result["warnings"] = list(authored.result.get("warnings") or []) + guard_warnings
        content = authored.updated.encode("utf-8")
        content_sha256 = self.api.upload(content)
        proposal_ids = [entry["proposal_id"] for entry in authored.result.get("proposals", [])] or [
            authored.result["proposal_id"]]
        figure_writes = []
        for figure in figure_list:
            figure_source = figures.figure_path(path, figure.name)
            owner = proposal_ids[figure.entry]
            existing = self.api.source(figure_source)
            if existing is not None and not existing.get("deleted"):
                current_owner = figures.owner_of(self.api.download(existing["object_sha256"]))
                if validated.create or current_owner != owner:
                    raise figures.refused(
                        f"{figure_source} already exists; choose another figure name.")
            figure_content = figures.owned_content(figure, owner)
            if existing is not None and existing.get("deleted"):
                # A deleted path keeps its revision; its content is empty.
                existing = {"revision": existing["revision"], "sha256": sha256_hex(b""),
                            "object_sha256": sha256_hex(b"")}
            figure_writes.append((figure_source, existing, figure_content, self.api.upload(figure_content), figure))
        # Read the version again right before publishing: a change since the first
        # read refuses, like a stale expected_source_sha256, instead of merging.
        latest = self.api.source(path)
        if latest is None or latest.get("deleted"):
            raise authoring.AuthoringRefused(
                "source_conflict", "The source was removed while the proposal was written.", current_source_sha256="")
        if latest["sha256"] != snapshot["sha256"] or latest["revision"] != snapshot["revision"]:
            raise authoring.AuthoringRefused(
                "source_conflict", "The source changed while the proposal was written; read it again.",
                current_source_sha256=latest["sha256"])
        sequence = self.api.device_sequence()
        next_sequence = int(sequence["next_device_sequence"])
        if not 1 <= next_sequence <= MAXIMUM_DEVICE_SEQUENCE:
            raise authoring.AuthoringRefused("write_failed", "The server returned an invalid device sequence.")
        empty_sha256 = sha256_hex(b"")
        if figure_writes:
            self.api.upload(b"")
        publication = {
            "publication_id": str(uuid.uuid4()),
            "mutations": [{
                "action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": next_sequence,
                "path": path, "base_revision": int(snapshot["revision"]), "base_sha256": snapshot["sha256"],
                "base_object_sha256": snapshot["object_sha256"], "client_timestamp": client_timestamp(),
                "content_sha256": content_sha256,
            }] + [{
                # The note's figures are published in the same publication: all or nothing.
                "action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": next_sequence + 1 + index,
                "path": figure_source, "base_revision": int(base["revision"]) if base else 0,
                "base_sha256": base["sha256"] if base else empty_sha256,
                "base_object_sha256": base["object_sha256"] if base else empty_sha256,
                "client_timestamp": client_timestamp(), "content_sha256": figure_sha256,
            } for index, (figure_source, base, _content, figure_sha256, _figure) in enumerate(figure_writes)],
        }
        if authored.claims:
            # The withdrawal travels as a decision claim on the upload that removes the wrapper
            # (protocol/agent-proposals.md 10.4.2), so the server retires it as withdrawn instead of
            # "removed outside Thoughts". It is part of the stored publication, so a retry resends it.
            decided_at = client_timestamp()
            publication["proposal_decisions"] = [
                {"decision_id": str(uuid.uuid4()), **claim, "decided_at": decided_at, "origin": "thoughts-cli"}
                for claim in authored.claims]
        result = authored.result
        if figure_writes:
            result = {**result, "figures": [
                {"name": figure.name, "path": figure_source, "proposal_id": proposal_ids[figure.entry],
                 "reference": figures.figure_reference(path, figure.name)}
                for figure_source, _base, _content, _sha, figure in figure_writes]}
        record = {
            "status": "prepared", "request_sha256": digest, "command": validated.command, "source": path,
            "request_id": validated.request_id, "device_id": sequence.get("device_id"),
            "publication": publication, "content_sha256": content_sha256, "result": result,
            "figure_sha256s": [figure_sha256 for _source, _base, _content, figure_sha256, _figure in figure_writes],
        }
        self.store.keep_content(content_sha256, content)
        for _source, _base, figure_content, figure_sha256, _figure in figure_writes:
            self.store.keep_content(figure_sha256, figure_content)
        self.store.put(key, record)
        return self._publish(key, record)

    def add_html_page(self, request: Any) -> dict[str, Any]:
        """add-html-page: add or replace one HTML page of a docs folder (html_pages.py); never raises for a refusal."""
        try:
            return html_pages.add_page(self.api, request)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    # ------------------------------------------------------------------ builds and PDFs

    def build_status(self, request: Any) -> dict[str, Any]:
        """build-status: why a document's PDF does not build, and the pending proposal most likely to blame
        (build_status.py); only reads, never raises for a refusal."""
        try:
            return build_status.build_status(self.api, request)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    def move_pdf(self, request: Any) -> dict[str, Any]:
        """move-pdf: move an imported PDF to another Project (pdf_moves.py); never raises for a refusal.

        The request record keeps the action and its operation_id before it is sent, so a retry with the
        same request_id is answered from the server's receipt and never moves twice."""
        try:
            validated = pdf_moves.validate(request)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        digest = authoring.request_sha256(request)
        key = f"{self.scope}#{validated['request_id']}"
        try:
            with self.store.locked():
                record = self.store.get(key)
                if record is not None:
                    if record.get("request_sha256") != digest:
                        return error_body("request_conflict", "This request ID was already used with different arguments.")
                    if record.get("status") == "applied":
                        return {**record["result"], "replayed": True}
                else:
                    record = {"status": "prepared", "command": pdf_moves.MOVE_PDF, "request_sha256": digest,
                              "request_id": validated["request_id"], "move": None}
                return self._move_pdf(key, record, validated)
        except Busy as busy:
            return error_body("busy", str(busy))
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    def _move_pdf(self, key: str, record: dict[str, Any], validated: dict[str, str]) -> dict[str, Any]:
        catalogue = None
        for _attempt in range(MAX_PROJECT_ATTEMPTS):
            if record.get("move") is None:
                catalogue = self.api.library_projects()
                source, pdf = pdf_moves.find_pdf(catalogue, validated["pdf"])
                destination = documents.resolve_project(catalogue, validated["project"])
                if destination["id"] == source["id"]:
                    self.store.delete(key)
                    raise authoring.AuthoringRefused(
                        "no_change", f"{pdf.get('filename')} is already in Project {source.get('name')!r}.")
                record.update(move=pdf_moves.change(source, pdf, destination, str(uuid.uuid4())),
                              filename=pdf.get("attachment_filename") or pdf.get("filename"), name=pdf.get("name"),
                              from_project=source.get("name"), to_project=destination.get("name"))
                self.store.put(key, record)
            try:
                catalogue = self.api.library_change(record["move"])
                break
            except ApiError as error:
                if error.status != 409:
                    raise
                # Refused: nothing moved. A Project changed meanwhile: read the revisions again.
                record["move"] = None
                self.store.put(key, record)
                if PROJECT_CHANGED in error.detail:
                    continue
                self.store.delete(key)
                raise authoring.AuthoringRefused("write_failed", f"Thoughts did not move the PDF: {error.detail}") \
                    from error
        else:
            self.store.delete(key)
            raise authoring.AuthoringRefused(
                "project_conflict", "The Projects kept changing on another device; nothing moved. Retry later.",
                retryable=True)
        now_in = pdf_moves.holder(catalogue, record["move"]["document_id"])
        if now_in is None or now_in.get("id") != record["move"]["destination_project_id"]:
            raise authoring.AuthoringRefused(
                "write_failed", "The server answered, but its catalogue does not show the PDF in the destination; "
                "retry with the identical request and request_id.", retryable=True)
        result = {
            "schema_version": 1, "accepted": True, "terminal": True, "state": "pdf_moved",
            "document_id": record["move"]["document_id"], "filename": record["filename"], "name": record["name"],
            "from_project": record["from_project"], "to_project": record["to_project"],
            "project_id": now_in.get("id"), "project_revision": now_in.get("revision"),
            "request_id": record["request_id"], "replayed": False,
            "linking_sources": self._linking_sources(record["filename"][:-4]),
            "message": (f"Moved {record['filename']} from Project {record['from_project']!r} to "
                        f"{record['to_project']!r}, with its review cards. Links to it keep working: \\PdfLink and "
                        "\\CrossPdfLink find a Library PDF by its filename. The phone and Mac see the move after "
                        "their next Sync."),
        }
        if self.render_check:
            result["render_check"] = self._render_check(record["request_id"])
        self.store.put(key, {**record, "status": "applied", "result": result})
        return result

    def _linking_sources(self, key: str) -> list[str]:
        """The LaTeX and Markdown sources that name the PDF key (best effort; uses the object cache)."""
        found = []
        try:
            listing = self.api.sources()
            for row in listing.get("sources", []):
                if row.get("deleted") or source_format(row["path"]) is None:
                    continue
                if key.encode("utf-8") in self.cache.get(row["object_sha256"]):
                    found.append(row["path"])
        except (ApiError, TransportError):
            pass
        return found

    def _ids_elsewhere(self, path: str) -> Callable[[Iterable[str]], str | None]:
        """The desktop's workspace uniqueness check, over every canonical source of the server."""

        def check(ids: Iterable[str]) -> str | None:
            wanted = set(ids)
            encoded = [ident.encode("utf-8") for ident in wanted]
            listing = self.api.sources()
            found = None
            keep = set()
            for row in listing.get("sources", []):
                if row.get("deleted") or source_format(row["path"]) is None:
                    continue
                keep.add(row["object_sha256"])
                if row["path"] == path or found is not None:
                    continue
                try:
                    content = self.cache.get(row["object_sha256"])
                except (ApiError, TransportError) as error:
                    raise authoring.AuthoringRefused(
                        "read_failed", f"Cannot check proposal ID uniqueness in {row['path']}: {error}") from error
                if not any(ident in content for ident in encoded):
                    continue
                if authoring._wrapper_ids(row["path"], content.decode("utf-8", "replace")) & wanted:
                    found = row["path"]
            self.cache.prune(keep)
            return found

        return check

    # ------------------------------------------------------------------ review cards

    def review_card(self, request: Any) -> dict[str, Any]:
        """Create a card from a source selection or move a card into a note (review_cards.py);
        never raises for a refusal."""
        try:
            validated = review_cards.validate_request(request)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        digest = authoring.request_sha256(request)
        key = f"{self.scope}#{validated.request_id}"
        try:
            with self.store.locked():
                record = self.store.get(key)
                if record is not None:
                    if record.get("request_sha256") != digest:
                        return error_body("request_conflict", "This request ID was already used with different arguments.")
                    if record.get("status") == "applied":
                        return {**record["result"], "replayed": True}
                    # Prepared: the answer to its operation was lost; send the same one again.
                    return self._send_card(key, record)
                return self._prepare_card(key, digest, validated)
        except Busy as busy:
            return error_body("busy", str(busy))
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    def _prepare_card(self, key: str, digest: str, validated: review_cards.CardRequest) -> dict[str, Any]:
        move = validated.command == review_cards.MOVE_REVIEW_CARD
        now = self.now()
        due = None if move else review_cards.parse_due_at(validated.due_at)
        if due is not None and review_cards.due_in_past(due, now):
            raise authoring.AuthoringRefused("invalid_request", "Choose now or a future review time.")
        capabilities = self.api.health().get("capabilities") or []
        missing = [name for name in review_cards.CAPABILITIES[validated.command] if name not in capabilities]
        if missing:
            raise authoring.AuthoringRefused(
                "server_unsupported", f"The server cannot take this card yet ({', '.join(missing)}); nothing was written.")
        path = validated.source
        snapshot, raw = self._read(path)
        try:
            text = raw.decode("utf-8") if len(raw) <= review_cards.MAX_SOURCE_BYTES else None
        except UnicodeDecodeError:
            text = None
        if text is None or "\0" in text:
            raise authoring.AuthoringRefused("invalid_source", "Cards need a valid UTF-8 source of at most 4 MiB without NUL.")
        if authoring.is_generated_source(authoring.owner_relative(path), text):
            raise authoring.AuthoringRefused(
                "generated_source", "This file is generated and its card markers would be lost; "
                "mark the passage in the file it is generated from.")
        if snapshot["sha256"] != validated.expected_source_sha256:
            # A retry of a move the server keeps as pending: deciding the proposal changed the source.
            if move and card_pending_moves.CAPABILITY in capabilities and (
                    replayed := self._pending_replay(validated)) is not None:
                return replayed
            raise authoring.AuthoringRefused("source_conflict", "The source changed; select the passage again.",
                                             current_source_sha256=snapshot["sha256"])
        document = review_cards.owner_document(self.api.library_projects(), path)
        row = review_cards.card_projection(self.api.projections(), validated.card_id)
        if move:
            current, from_stem, to_stem = review_cards.check_movable(row, document)
            target = review_cards.pending_target(path, text, validated.start_line, validated.end_line, validated.card_id)
            if isinstance(target, str):
                raise authoring.AuthoringRefused("invalid_selection", target)
            if target is not None:
                # Proposed text: the server moves the card once the user accepts the proposal.
                if card_pending_moves.CAPABILITY not in capabilities:
                    raise authoring.AuthoringRefused(
                        "invalid_selection", review_cards.PENDING_PROPOSAL + " This Thoughts server cannot wait for "
                        f"the proposal yet ({card_pending_moves.CAPABILITY}).")
                return card_pending_moves.create(self.api, card_pending_moves.body(dataclasses.asdict(validated)))
        elif row is not None:
            raise authoring.AuthoringRefused(
                "request_conflict", "A card with this request ID already exists; use a new request_id for a new card.")
        marked, anchor = review_cards.mark(path, text, validated.start_line, validated.end_line, validated.card_id)
        content = marked.encode("utf-8")
        before_sha256, content_sha256 = self.api.upload(raw), self.api.upload(content)
        # Read the version again right before sending, as for proposals: a change refuses.
        latest = self.api.source(path)
        if latest is None or latest.get("deleted") or latest["sha256"] != snapshot["sha256"]:
            raise authoring.AuthoringRefused(
                "source_conflict", "The source changed while the card was prepared; select the passage again.",
                current_source_sha256=(latest or {}).get("sha256", ""))
        sequence = self.api.device_sequence()
        next_sequence = int(sequence["next_device_sequence"])
        if not 1 <= next_sequence <= MAXIMUM_DEVICE_SEQUENCE:
            raise authoring.AuthoringRefused("write_failed", "The server returned an invalid device sequence.")
        result: dict[str, Any] = {
            "schema_version": 1, "accepted": True, "terminal": True, "state": "card_moved" if move else "card_created",
            "card_id": validated.card_id, "request_id": validated.request_id, "document": document["filename"],
            "document_id": document["document_id"], "source": path, "source_sha256": content_sha256,
            "source_link_state": "linked", "pdf_placement_state": "pending", "replayed": False,
            "message": ("Card moved with its history; its passage is marked in the source. "
                        if move else "Card created; its passage is marked in the source. ")
                       + "Its PDF areas are placed when the note is next built.",
        }
        if move:
            result["from_document"] = f"{from_stem}.pdf"
            payload = review_cards.moved_card(current, document, anchor, from_stem, to_stem, now, validated, digest,
                                              dict(result))
            base_revision = int(row["revision"])
        else:
            result["due_at"] = review_cards.utc_string(due)
            payload = review_cards.created_card(validated, document, anchor, due, now, digest, dict(result))
            base_revision = 0
        # A creation's transaction is the card's (its ID is the request's); a move's is the request's.
        payload["source_change"] = {"path": path, "transaction_id": validated.request_id,
                                    "before_sha256": before_sha256, "after_sha256": content_sha256}
        operation = {
            "operation_id": str(uuid.uuid4()), "device_sequence": next_sequence, "entity_type": "review_card",
            "entity_id": validated.card_id, "collection": "cards", "base_revision": base_revision,
            "client_timestamp": client_timestamp(), "payload": payload,
        }
        record = {
            "status": "prepared", "request_sha256": digest, "command": validated.command, "source": path,
            "request_id": validated.request_id, "card_id": validated.card_id, "device_id": sequence.get("device_id"),
            "operation": operation, "before_sha256": before_sha256, "content_sha256": content_sha256, "result": result,
        }
        self.store.keep_content(before_sha256, raw)
        self.store.keep_content(content_sha256, content)
        self.store.put(key, record)
        return self._send_card(key, record)

    def _pending_replay(self, validated: review_cards.CardRequest) -> dict[str, Any] | None:
        """The server's answer to a move this request ID already recorded as pending, or None.

        Once the proposal is decided the source has changed, and a retry must still be answered
        from the server's record (or refused as request_conflict)."""
        if self.api.pending_card_move(validated.request_id) is None:
            return None
        return card_pending_moves.create(self.api, card_pending_moves.body(dataclasses.asdict(validated)))

    def _send_card(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        """POST the record's card operation until the server answers it, as _send_publication does."""
        operation = record["operation"]
        reuploaded = False
        recoveries = 0
        while True:
            try:
                answer = self.api.operations([operation])
                break
            except ApiError as error:
                if (error.status == 409 and "is already assigned to another operation" in error.detail
                        and recoveries < MAX_SEQUENCE_RECOVERIES):
                    # Another operation of this device owns the number and the card was not applied.
                    recoveries += 1
                    operation["device_sequence"] = int(self.api.device_sequence()["next_device_sequence"])
                    record["operation"] = operation
                    self.store.put(key, record)
                    continue
                if error.status == 409 and "Upload the verified review source objects first" in error.detail and not reuploaded:
                    reuploaded = True
                    for digest in (record.get("before_sha256"), record.get("content_sha256")):
                        if (content := self.store.content(digest)) is not None:
                            self.api.upload(content)
                    continue
                if error.status in (409, 422):
                    # The server refused the card and its source change together; nothing was written.
                    self.store.delete(key)
                    raise self._card_refused(record, error) from error
                raise
        results = answer.get("results") or []
        applied = results[0] if results else {}
        if applied.get("outcome") not in ("applied", "duplicate"):
            self.store.delete(key)
            raise authoring.AuthoringRefused(
                "source_conflict", "The card changed on another device meanwhile; nothing was written. "
                "Look the card up again.")
        result = {**record["result"], "card_revision": applied.get("entity_revision")}
        if self.render_check:
            result["render_check"] = self._render_check(record["request_id"])
        self.store.put(key, {**record, "status": "applied", "result": result})
        self.store.drop_content(record.get("before_sha256"))
        self.store.drop_content(record.get("content_sha256"))
        return result

    def _card_refused(self, record: dict[str, Any], error: ApiError) -> authoring.AuthoringRefused:
        if "conflict" in error.detail or "changed" in error.detail or "merge base" in error.detail:
            current = self.api.source(record["source"])
            return authoring.AuthoringRefused(
                "source_conflict", f"The source or the card changed meanwhile, so nothing was written ({error.detail}). "
                "Read the source again and select the passage.",
                current_source_sha256=current["sha256"] if current and not current.get("deleted") else "")
        return authoring.AuthoringRefused("write_failed", f"The server refused the card, nothing was written: {error.detail}")

    def merge_review_cards(self, request: Any) -> dict[str, Any]:
        """merge-review-cards: the server merges the cards (card_merges.py); never raises for a refusal.

        The server answers a retry of the same request_id from its receipt, so a lost answer is retried
        with the identical request and never merges twice."""
        try:
            body = card_merges.validate(request)
            result = card_merges.merge(self.api, body)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)
        if self.render_check and not result.get("replayed"):
            # The PDF rebuild places the kept card's areas (the Mac asks for it after each Sync).
            result = {**result, "render_check": self._render_check(body["request_id"])}
        return result

    def pending_card_moves(self, request: Any) -> dict[str, Any]:
        """pending-card-moves and cancel-pending-card-move (card_pending_moves.py); never raises for a refusal."""
        try:
            return card_pending_moves.run(self.api, card_pending_moves.validate(request))
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    # ------------------------------------------------------------------ new documents

    def create_document(self, request: Any) -> dict[str, Any]:
        """Add a new Markdown or LaTeX note to a Library Project (create-document); never raises for a refusal.

        The request record goes through the stages publish (the new source), include
        (include_source), rename (rename_document) and inputs (the PDF's build inputs), each
        saved before it is sent, so a retry with the same request_id resumes where the answer
        was lost and never creates a second document."""
        try:
            validated = documents.validate(request)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        digest = authoring.request_sha256(request)
        key = f"{self.scope}#{validated.request_id}"
        try:
            with self.store.locked():
                record = self.store.get(key)
                if record is not None:
                    if record.get("request_sha256") != digest:
                        return error_body("request_conflict", "This request ID was already used with different arguments.")
                    if record.get("status") == "applied":
                        return {**record["result"], "replayed": True}
                    # Prepared: an answer was lost; continue from the recorded stage.
                    return self._finish_document(key, record)
                return self._start_document(key, digest, validated)
        except Busy as busy:
            return error_body("busy", str(busy))
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    def _start_document(self, key: str, digest: str, validated: documents.DocumentRequest) -> dict[str, Any]:
        project = documents.resolve_project(self.api.library_projects(), validated.project)
        path = documents.default_folder(project) + "/" + validated.filename
        if len(path) > MAX_SOURCE_PATH:
            raise documents.invalid(f"The document's path would exceed {MAX_SOURCE_PATH} characters; shorten filename.")
        if documents.find_document(project, path) is not None:
            raise authoring.AuthoringRefused(
                "source_exists", f"{path} is already a document of the Project {project.get('name')!r}. Existing "
                "notes change only through proposals; choose another filename for a new one.", source=path)
        existing = self.api.source(path)
        if existing is not None and not existing.get("deleted"):
            raise authoring.AuthoringRefused(
                "source_exists", f"{path} already exists. Existing notes change only through proposals; "
                "choose another filename for a new one.", source=path)
        # The checks proposals get (guards.py, pdf_links.py), on the whole new note.
        checked, guard_warnings = guards.rewrite_include_paths(path, validated.content)
        guards.check_markdown(path, checked, what="new document")
        guard_warnings += pdf_links.check(self.api.library_projects, path, "", checked)
        validated = dataclasses.replace(validated, content=checked)
        content = documents.content_bytes(validated)
        content_sha256 = self.api.upload(content)
        # The base of a new source is the empty object; the server needs it uploaded.
        self.api.upload(b"")
        sequence = self.api.device_sequence()
        next_sequence = int(sequence["next_device_sequence"])
        if not 1 <= next_sequence <= MAXIMUM_DEVICE_SEQUENCE:
            raise authoring.AuthoringRefused("write_failed", "The server returned an invalid device sequence.")
        publication = {
            "publication_id": str(uuid.uuid4()),
            "mutations": [{
                # A new source: revision 0 and the empty base. A deleted path keeps its
                # revision, and its content is empty (thoughts_server.sources).
                "action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": next_sequence,
                "path": path, "base_revision": int(existing["revision"]) if existing is not None else 0,
                "base_sha256": EMPTY_SHA256, "base_object_sha256": EMPTY_SHA256,
                "client_timestamp": client_timestamp(), "content_sha256": content_sha256,
            }],
        }
        record = {
            "status": "prepared", "stage": "publish", "command": documents.CREATE_DOCUMENT, "request_sha256": digest,
            "request_id": validated.request_id, "conversation_id": validated.conversation_id,
            "project_id": project["id"], "project": project.get("name"), "name": validated.name,
            "filename": validated.filename, "format": validated.format, "source": path,
            # Android's include_source: the client names the new document.
            "document_id": str(uuid.uuid4()), "device_id": sequence.get("device_id"),
            "publication": publication, "content_sha256": content_sha256,
            "include": None, "rename": None, "warnings": documents.warnings(validated) + guard_warnings,
        }
        self.store.keep_content(content_sha256, content)
        self.store.put(key, record)
        return self._finish_document(key, record)

    def _finish_document(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        catalogue = None
        if record["stage"] == "publish":
            path = record["source"]
            answer = self._send_publication(key, record, lambda error: self._document_publication_refused(path, error))
            results = answer.get("results") or []
            applied = results[0] if results else {}
            outcome = applied.get("outcome")
            if outcome not in ("applied", "duplicate", "merged"):
                self.store.delete(key)
                raise authoring.AuthoringRefused("write_failed", f"Unexpected publication outcome {outcome!r}.")
            content_sha256 = record.pop("content_sha256", None)
            if outcome == "merged" and applied.get("sha256") != content_sha256:
                record["warnings"].append({
                    "code": "merged_with_concurrent_change",
                    "message": "Another device created this file at the same moment and the server merged both "
                               "versions; read it with read-source."})
            record.update(stage="include", source_sha256=applied.get("sha256"), source_revision=applied.get("revision"),
                          source_frontier=answer.get("source_frontier"), publication_id=answer.get("publication_id"),
                          publication_outcome=outcome, published_content_sha256=content_sha256)
            self.store.put(key, record)
            self.store.drop_content(content_sha256)
        # Then include (declare), inputs (the PDF's snapshot, right after the declaration),
        # hide (unpublish when it has none), rename. A published note without a snapshot
        # would fail every build of the Library, so it is never left so.
        while record["stage"] != "done":
            stage = record["stage"]
            if stage == "include":
                catalogue = self._include_document(key, record)
                record["stage"] = "inputs"
            elif stage == "inputs":
                record["build_inputs"] = self._new_document_inputs(key, record, catalogue)
                record["published"] = self._has_inputs(record)
                record["stage"] = "hide" if not record["published"] else self._after(record)
            elif stage == "hide":
                catalogue = self._library_step(key, record, "hide", catalogue)
                record["published"] = False
                record["stage"] = self._after(record)
            elif stage == "rename":
                catalogue = self._library_step(key, record, "rename", catalogue)
                project = documents.project_by_id(catalogue, record["project_id"])
                if project is not None:
                    record.update(project=project.get("name"), project_revision=project.get("revision"))
                record["renamed"] = True
                # A record from before the inputs stage: its inputs come after its title.
                record["stage"] = "inputs" if "build_inputs" not in record else "done"
            else:
                raise authoring.AuthoringRefused("write_failed", f"Unknown request stage {stage!r}.")
            self.store.put(key, record)
        result = {
            "schema_version": 1, "accepted": True, "terminal": True, "state": "document_created",
            "document_id": record["document_id"], "project_id": record["project_id"], "project": record["project"],
            "project_revision": record.get("project_revision"), "name": record["name"], "source": record["source"],
            "filename": record["filename"], "format": record["format"], "source_sha256": record["source_sha256"],
            "source_revision": record["source_revision"], "source_frontier": record.get("source_frontier"),
            "publication_id": record["publication_id"], "publication_outcome": record["publication_outcome"],
            "conversation_id": record["conversation_id"], "request_id": record["request_id"], "replayed": False,
            "published": record.get("published", True),
        }
        if record.get("device_id"):
            result["device_id"] = record["device_id"]
        if record.get("build_inputs") is not None:
            result["build_inputs"] = record["build_inputs"]
        warnings = list(record.get("warnings") or []) + list((record.get("inputs") or {}).get("warnings") or [])
        if not result["published"]:
            warnings.append({"code": "document_unpublished", "message": (
                "The note was created unpublished, because Thoughts could not give its PDF build inputs "
                f"({record['build_inputs']['message']}); a published note without them would stop every build. "
                "Publish it once it has them: thoughts-cli publish-build-inputs with publish, or in Thoughts after "
                "the Mac's next Sync.")})
        if warnings:
            result["warnings"] = warnings
        if self.render_check:
            result["render_check"] = (self._render_check(record["request_id"]) if result["published"] else {
                "status": "skipped", "message": "The note is unpublished, so the server does not build it."})
        self.store.put(key, {**record, "status": "applied", "result": result})
        return result

    @staticmethod
    def _after(record: dict[str, Any]) -> str:
        return "done" if record.get("renamed") else "rename"

    @staticmethod
    def _has_inputs(record: dict[str, Any]) -> bool:
        """Whether the PDF has a registered build-input snapshot (a render check can build it)."""
        return (record.get("build_inputs") or {}).get("status") in {"published", "current", "kept"}

    def _include_document(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        """include_source, with the new PDF's snapshot composed before it is sent (so it follows at once)."""
        catalogue = None
        if "inputs" not in record:
            catalogue = self.api.library_projects()
            project = documents.project_by_id(catalogue, record["project_id"])
            if project is None:
                raise self._project_gone(record)
            try:
                note = build_inputs.planned_note(project, record["source"], record["document_id"], record["format"])
                record["inputs"] = self._composed(note)
            except authoring.AuthoringRefused as refusal:
                record["inputs"] = {"error": {"error_code": refusal.code, "message": refusal.message}}
            self.store.put(key, record)
        catalogue = self._library_step(key, record, "include", catalogue)
        project = documents.project_by_id(catalogue, record["project_id"])
        document = documents.find_document(project, record["source"]) if project else None
        if document is None or not isinstance(document.get("document_id"), str):
            record["include"] = None
            self.store.put(key, record)
            raise authoring.AuthoringRefused(
                "write_failed", f"The note was saved as {record['source']}, but the Project does not list it; "
                "retry with the identical request and request_id.", retryable=True)
        # The server keeps the identity of a source declared before (a no-op include).
        record.update(document_id=document["document_id"], project=project.get("name"),
                      project_revision=project.get("revision"))
        return catalogue

    def _composed(self, note: build_inputs.Note) -> dict[str, Any]:
        payload, warnings = self._compose_build_inputs(note)
        return {"payload": payload, "sha256": build_inputs.snapshot_sha256(payload), "warnings": warnings}

    def _new_document_inputs(self, key: str, record: dict[str, Any],
                             catalogue: dict[str, Any] | None) -> dict[str, Any]:
        """The new note's snapshot, sent right after include_source; a summary with status skipped or
        refused (error_code, message) when it has none, so the note is unpublished."""
        inputs = record.get("inputs") or {}
        try:
            # The declared note, as the server answered; compose again if it differs from the plan.
            note = build_inputs.find_note(catalogue or self.api.library_projects(), build_inputs.InputsRequest(
                request_id=record["request_id"], document_id=record["document_id"], project=None, filename=None))
            payload = inputs.get("payload")
            if payload is None or (payload["target"], payload["project"]) != (note.target, note.folder):
                inputs = record["inputs"] = self._composed(note)
                self.store.put(key, record)
        except authoring.AuthoringRefused as refusal:
            error = inputs.get("error") or {"error_code": refusal.code, "message": refusal.message}
            return {"status": "skipped", **error}
        try:
            return self._send_build_inputs(key, record)
        except ApiError as error:
            if error.status not in (409, 422):
                raise
            return {"status": "refused", "error_code": "build_inputs_refused",
                    "message": f"The server refused the build inputs: {error.detail}"}

    # ------------------------------------------------------------------ build inputs

    def publish_build_inputs(self, request: Any) -> dict[str, Any]:
        """Publish the build inputs of an existing note's PDF (publish-build-inputs); never raises for a refusal.

        For a Markdown or standalone LaTeX note of a Project's default folder whose PDF has no
        build inputs yet (created by an agent or the phone before the Mac synced), published or
        not. Inputs that exist are never replaced (status kept); no other PDF's inputs are
        touched. With publish, an unpublished note is published (set_document_published) only
        once its PDF has inputs, so it never stops the Library's builds."""
        try:
            validated = build_inputs.validate(request)
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        digest = authoring.request_sha256(request)
        key = f"{self.scope}#{validated.request_id}"
        try:
            with self.store.locked():
                record = self.store.get(key)
                if record is not None:
                    if record.get("request_sha256") != digest:
                        return error_body("request_conflict", "This request ID was already used with different arguments.")
                    if record.get("status") == "applied":
                        return {**record["result"], "replayed": True}
                    return self._finish_build_inputs(key, record)
                note = build_inputs.find_note(self.api.library_projects(), validated)
                payload, warnings = self._compose_build_inputs(note)
                record = {
                    "status": "prepared", "stage": "inputs", "command": build_inputs.PUBLISH_BUILD_INPUTS,
                    "request_sha256": digest, "request_id": validated.request_id, "document_id": note.document_id,
                    "project_id": note.project_id, "project": note.project.get("name"), "source": note.source,
                    "format": note.format, "publish": validated.publish, "published": note.published, "show": None,
                    "inputs": {"payload": payload, "sha256": build_inputs.snapshot_sha256(payload),
                               "warnings": warnings},
                }
                self.store.put(key, record)
                return self._finish_build_inputs(key, record)
        except Busy as busy:
            return error_body("busy", str(busy))
        except authoring.AuthoringRefused as refusal:
            return refusal.body()
        except (ApiError, TransportError) as error:
            return self._transport_failure(error)

    def _finish_build_inputs(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("stage", "inputs") == "inputs":
            try:
                record["build_inputs"] = self._send_build_inputs(key, record)
            except ApiError as error:
                if error.status not in (409, 422):
                    raise
                # Refused: nothing was registered, and the note was not published; a retry
                # composes the inputs again.
                self.store.delete(key)
                raise authoring.AuthoringRefused(
                    "build_inputs_refused", f"The server refused the build inputs of {record['source']}; nothing was "
                    f"published{'' if record.get('published') else ' and the note stays unpublished'}: "
                    f"{error.detail}", source=record["source"]) from error
            record["stage"] = "show" if record.get("publish") and not record.get("published") else "done"
            self.store.put(key, record)
        if record["stage"] == "show":
            catalogue = self._library_step(key, record, "show")
            project = documents.project_by_id(catalogue, record["project_id"])
            record.update(stage="done", published=True,
                          **({"project_revision": project.get("revision")} if project else {}))
            self.store.put(key, record)
        summary = record["build_inputs"]
        result = {
            "schema_version": 1, "accepted": True, "terminal": True, "state": "build_inputs_published",
            "document_id": record["document_id"], "project_id": record["project_id"], "project": record["project"],
            "source": record["source"], "format": record["format"], "target": summary["target"],
            "build_inputs": summary, "published": record.get("published", True), "request_id": record["request_id"],
            "replayed": False,
        }
        if record["inputs"].get("warnings"):
            result["warnings"] = record["inputs"]["warnings"]
        if self.render_check:
            result["render_check"] = (self._render_check(record["request_id"]) if result["published"] else {
                "status": "skipped", "message": "The note is unpublished, so the server does not build it; publish it "
                                                "(publish: true) to build it."})
        self.store.put(key, {**record, "status": "applied", "result": result})
        return result

    def _compose_build_inputs(self, note: build_inputs.Note) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read what the server knows of the note's folder and compose its PDF's snapshot (build_inputs.compose)."""
        listing = self.api.sources()
        live = {row["path"]: row for row in listing.get("sources", [])
                if isinstance(row, dict) and isinstance(row.get("path"), str) and not row.get("deleted")}
        current = live.get(note.source)
        if current is None:
            raise build_inputs.unsupported(f"{note.source} is not a current source of this Library.", source=note.source)
        if note.format == "latex":
            text = self.api.download(current["object_sha256"]).decode("utf-8", "replace")
            if not documents.standalone_latex(text):
                raise build_inputs.unsupported(
                    f"{note.source} is not a standalone LaTeX document (\\documentclass and \\begin{{document}}).",
                    source=note.source)
        rules = live.get(note.folder + "/.ThoughtIgnore")
        try:
            ignore = build_inputs.ThoughtIgnore(self.api.download(rules["object_sha256"]) if rules else None)
        except (ValueError, UnicodeDecodeError) as error:
            raise build_inputs.unsupported(f"The folder's .ThoughtIgnore cannot be read: {error}") from None
        snapshots = [snapshot for target in build_inputs.folder_targets(note)
                     if (snapshot := self.api.latest_build_inputs(target)) is not None]
        return build_inputs.compose(note, live, snapshots, ignore, self.api.object_info)

    def _send_build_inputs(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        """Register the record's snapshot unless the PDF has one (desktop publishBuildInputs).

        The same snapshot is already current; a different one (the Mac's, or an earlier
        request's) is kept, never replaced. Refusals (409/422) propagate as ApiError."""
        inputs = record["inputs"]
        payload = inputs["payload"]
        ours = {inputs["sha256"], inputs.get("published_sha256")} - {None}
        latest = self.api.latest_build_inputs(payload["target"])
        if latest is not None and latest.get("sha256") in ours:
            return build_inputs.summary(payload, latest["sha256"], "published" if inputs.get("posted") else "current")
        if latest is not None:
            kept = build_inputs.summary(payload, latest.get("sha256"), "kept")
            kept.update(entries=len(latest.get("entries") or []), discovery_policy=latest.get("discovery_policy"),
                        project_folder=latest.get("project"),
                        message="This PDF already has build inputs (from the Mac or an earlier request); they were "
                                "kept unchanged.")
            kept.pop("roles", None)
            return kept
        inputs["posted"] = True
        self.store.put(key, record)
        answer = self.api.publish_build_inputs(payload)
        inputs["published_sha256"] = answer.get("sha256") if isinstance(answer, dict) else None
        self.store.put(key, record)
        return build_inputs.summary(payload, inputs["published_sha256"], "published")

    def _document_publication_refused(self, path: str, error: ApiError) -> authoring.AuthoringRefused:
        current = self.api.source(path)
        if current is not None and not current.get("deleted"):
            return authoring.AuthoringRefused(
                "source_exists", f"{path} was created by another device at the same moment; nothing was "
                "published. Choose another filename.", source=path)
        return authoring.AuthoringRefused(
            "write_failed", f"The server refused the publication, nothing was written: {error.detail}")

    def _library_step(self, key: str, record: dict[str, Any], step: str,
                      catalogue: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send the record's include, rename, hide or show action and return the catalogue the server answers with.

        A recorded action is sent again unchanged: the server answers an operation it already
        applied from its receipt. One refused because the Project changed (409) is rebuilt on
        the current catalogue with a new operation ID, at most MAX_PROJECT_ATTEMPTS times."""
        for _attempt in range(MAX_PROJECT_ATTEMPTS):
            change = record.get(step)
            if change is None:
                if catalogue is None:
                    catalogue = self.api.library_projects()
                project = documents.project_by_id(catalogue, record["project_id"])
                if project is None:
                    raise self._project_gone(record)
                change = self._library_change(step, record, project)
                if change is None:
                    return catalogue
                record[step] = change
                self.store.put(key, record)
            try:
                return self.api.library_change(change)
            except ApiError as error:
                if error.status != 409:
                    raise
                # Refused: nothing of this action was applied.
                record[step] = None
                self.store.put(key, record)
                catalogue = None
                if PROJECT_CHANGED in error.detail:
                    continue
                if "no longer in the Library" in error.detail:
                    raise self._project_gone(record) from error
                extra: dict[str, Any] = {}
                if step == "include":
                    message = (f"The note was saved as {record['source']}, but Thoughts did not add it to the "
                               f"Project: {error.detail}")
                elif step == "hide":
                    message = (f"The note {record['source']} has no build inputs and Thoughts did not unpublish it "
                               f"({error.detail}); until it is, Library builds fail. Retry with the identical "
                               "request and request_id.")
                    extra["retryable"] = True
                elif step == "show":
                    message = (f"The build inputs of {record['source']} are registered, but Thoughts did not publish "
                               f"the note: {error.detail}")
                else:
                    message = (f"The note was added to the Project as {record['source']}, but Thoughts did not "
                               f"set its title: {error.detail}")
                raise authoring.AuthoringRefused("write_failed", message, source=record["source"], **extra) from error
        raise authoring.AuthoringRefused(
            "project_conflict", f"The Project kept changing on another device while the note ({record['source']}) "
            "was added. Retry with the identical request and request_id to finish; it is never created twice.",
            source=record["source"], retryable=True)

    @staticmethod
    def _library_change(step: str, record: dict[str, Any], project: dict[str, Any]) -> dict[str, Any] | None:
        """The include_source, rename_document or set_document_published action (hide, show) on the current
        Project, or None when it is done."""
        document = documents.find_document(project, record["source"])
        base = {"operation_id": str(uuid.uuid4()), "project_id": record["project_id"],
                "base_revision": int(project.get("revision") or 0)}
        if step == "include":
            if document is not None:
                return None
            return {**base, "action": "include_source", "source_file": record["source"],
                    "new_document_id": record["document_id"]}
        if document is None or not isinstance(document.get("document_id"), str):
            raise authoring.AuthoringRefused(
                "write_failed", f"The note {record['source']} is no longer in the Project.", source=record["source"])
        if step in {"hide", "show"}:
            if documents.published(project, document["document_id"]) == (step == "show"):
                return None
            return {**base, "action": "set_document_published", "document_id": document["document_id"],
                    "published": step == "show"}
        if documents.declared_name(project, document["document_id"]) == record["name"]:
            return None
        return {**base, "action": "rename_document", "document_id": document["document_id"], "name": record["name"]}

    @staticmethod
    def _project_gone(record: dict[str, Any]) -> authoring.AuthoringRefused:
        return authoring.AuthoringRefused(
            "unknown_project", f"The Project {record.get('project')!r} is no longer in the Library; the note was "
            f"saved as {record['source']} but is in no Project.", source=record["source"])

    # ------------------------------------------------------------------ publication

    def _send_publication(self, key: str, record: dict[str, Any],
                          refused: Callable[[ApiError], authoring.AuthoringRefused]) -> dict[str, Any]:
        """POST the record's publication until the server answers it; ``refused`` names a refusal (409/422)."""
        publication = record["publication"]
        reuploaded = False
        recoveries = 0
        while True:
            try:
                return self.api.publish(publication)
            except ApiError as error:
                if (error.status == 409 and error.detail == "device sequence is already assigned"
                        and error.error_code in (None, "device_sequence_assigned")
                        and recoveries < MAX_SEQUENCE_RECOVERIES):
                    # Another operation of this device owns the number and nothing of
                    # this publication was applied: renumber it, keeping its IDs.
                    recoveries += 1
                    following = error.json.get("next_device_sequence") if isinstance(error.json, dict) else None
                    if not isinstance(following, int):
                        following = int(self.api.device_sequence()["next_device_sequence"])
                    for index, mutation in enumerate(publication["mutations"]):
                        mutation["device_sequence"] = following + index
                    record["publication"] = publication
                    self.store.put(key, record)
                    continue
                if (error.status in (409, 422) and "must be uploaded first" in error.detail and not reuploaded
                        and (content := self.store.content(record.get("content_sha256"))) is not None):
                    reuploaded = True
                    self.api.upload(content)
                    self.api.upload(b"")
                    for figure_sha256 in record.get("figure_sha256s", []):
                        if (figure_content := self.store.content(figure_sha256)) is not None:
                            self.api.upload(figure_content)
                    continue
                if error.status in (409, 422):
                    # The server refused the whole publication; nothing was published.
                    self.store.delete(key)
                    raise refused(error) from error
                raise

    def _proposal_publication_refused(self, path: str, error: ApiError) -> authoring.AuthoringRefused:
        if "conflict" in error.detail or "changed" in error.detail:
            current = self.api.source(path)
            return authoring.AuthoringRefused(
                "source_conflict",
                "The source changed while the proposal was written and the changes overlap; "
                "nothing was published. Read it again.",
                current_source_sha256=current["sha256"] if current and not current.get("deleted") else "",
            )
        return authoring.AuthoringRefused(
            "write_failed", f"The server refused the publication, nothing was written: {error.detail}")

    def _publish(self, key: str, record: dict[str, Any]) -> dict[str, Any]:
        path = record["source"]
        answer = self._send_publication(key, record, lambda error: self._proposal_publication_refused(path, error))
        results = answer.get("results") or []
        applied = results[0] if results else {}
        outcome = applied.get("outcome")
        result = dict(record["result"])
        result.update({
            "source_revision": applied.get("revision"), "source_frontier": answer.get("source_frontier"),
            "publication_id": answer.get("publication_id"), "publication_outcome": outcome,
        })
        if record.get("device_id"):
            result["device_id"] = record["device_id"]
        if record.get("command") == authoring.REMOVE:
            decisions = answer.get("proposal_decisions")
            if isinstance(decisions, list):
                result["proposal_decisions"] = decisions
            if outcome == "merged":
                result = self._verify_withdrawn_merge(path, applied, result)
        if outcome == "merged" and record.get("command") != authoring.REMOVE:
            result = self._verify_merged(path, applied, result)
        elif outcome not in ("applied", "duplicate", "merged"):
            self.store.delete(key)
            raise authoring.AuthoringRefused("write_failed", f"Unexpected publication outcome {outcome!r}.")
        if self.render_check:
            result["render_check"] = self._render_check(record["request_id"])
        record = {**record, "status": "applied", "result": result}
        self.store.put(key, record)
        self.store.drop_content(record.get("content_sha256"))
        for figure_sha256 in record.get("figure_sha256s", []):
            self.store.drop_content(figure_sha256)
        return result

    def _verify_withdrawn_merge(self, path: str, applied: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """A concurrent change was merged with the withdrawal: check the proposal is gone from the merged version."""
        try:
            merged = self.api.download(applied["object_sha256"]).decode("utf-8")
        except (ApiError, TransportError, UnicodeDecodeError, KeyError) as error:
            verified, detail = False, f"the merged source could not be read ({error})"
        else:
            parsed = parse(path, merged)
            present = result["proposal_id"] in parsed.by_id() or any(
                issue.id == result["proposal_id"] for issue in parsed.issues)
            verified, detail = not present, ("" if not present else "the proposal is still in the merged source")
        result["source_sha256"] = applied.get("sha256", result["source_sha256"])
        warning = {
            "code": "merged_with_concurrent_change" if verified else "merged_withdrawal_unverified",
            "message": ("Another device changed this source at the same time; the server merged both changes and "
                        "the proposal is withdrawn. Read the source again before the next proposal."
                        if verified else
                        "Another device changed this source at the same time and the server merged both changes, "
                        f"but {detail}. Read the source again."),
        }
        result["warnings"] = [*result.get("warnings", []), warning]
        return result

    def _verify_merged(self, path: str, applied: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """A concurrent change was merged with the proposals: check they are intact and name the merged version."""
        try:
            merged = self.api.download(applied["object_sha256"]).decode("utf-8")
        except (ApiError, TransportError, UnicodeDecodeError, KeyError) as error:
            verified, merged = False, None
            detail = f"the merged source could not be read ({error})"
        else:
            parsed = parse(path, merged)
            by_id = parsed.by_id()
            entries = result.get("proposals") or [result]
            broken = [entry["proposal_id"] for entry in entries
                      if (found := by_id.get(entry["proposal_id"])) is None
                      or found.original != entry["original_text"] or found.replacement != entry["replacement"]
                      or found.created_at != entry["created_at"]
                      or any(issue.id == entry["proposal_id"] for issue in parsed.issues)]
            verified, detail = not broken, ("" if not broken else "changed proposals: " + ", ".join(broken))
            for entry in entries:
                if (found := by_id.get(entry["proposal_id"])) is not None:
                    entry["line"] = merged.count("\n", 0, found.start) + 1
        result["source_sha256"] = applied.get("sha256", result["source_sha256"])
        warning = {
            "code": "merged_with_concurrent_change" if verified else "merged_proposal_unverified",
            "message": ("Another device changed this source at the same time; the server merged both changes and "
                        "the proposals are intact. Read the source again before the next proposal."
                        if verified else
                        "Another device changed this source at the same time and the server merged both changes, "
                        f"but {detail}. Read the source again and check the proposals."),
        }
        result["warnings"] = [*result.get("warnings", []), warning]
        return result

    def _render_check(self, request_id: str) -> dict[str, Any]:
        """Ask the server to build what the new source made stale (the Mac does this after each Sync)."""
        try:
            answer = self.api.render_check("all", f"thoughts-cli:{request_id}")
        except (ApiError, TransportError) as error:
            return {"status": "failed", "message": str(error)}
        build = answer.get("build") if isinstance(answer, dict) else None
        summary = {"status": answer.get("status") if isinstance(answer, dict) else None}
        if isinstance(build, dict) and build.get("id"):
            summary["build_id"] = build["id"]
        return summary

    def _transport_failure(self, error: Exception) -> dict[str, Any]:
        if isinstance(error, ApiError) and error.status == 401:
            body = error_body("not_configured", "The server refused the device token (401).")
        elif isinstance(error, ApiError) and error.status >= 500:
            body = error_body("write_failed", f"The server failed: {error}")
            body["retryable"] = True
        else:
            body = error_body("transport_error", str(error))
            body["retryable"] = True
        if body.get("retryable"):
            body["message"] += " Retry with the identical request and request_id; it is never written twice."
        return body
