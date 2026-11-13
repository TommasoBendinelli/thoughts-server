# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The Thoughts server's public sync endpoints, as every device uses them (stdlib only).

Only the calls a proposal author needs: the source listing and one source
(GET /v1/sources[/<path>]), objects (GET/POST /v1/objects), the device's next
operation sequence (GET /v1/device-sequence), a source publication
(POST /v1/source-publications) and a conditional render check
(POST /v1/render-checks). The payloads are the ones the Mac's source publisher
(thoughts-desktop tools/publisher) and the SDKs (sdk/cpp, sdk/kotlin) send.
create-document also reads the Library-project catalogue and sends the
Library-project actions include_source and rename_document
(GET/POST /v1/library/projects), as the desktop and Android do. Build inputs
(build_inputs.py) read a PDF's latest build-input snapshot
(GET /v1/build-input-snapshots/latest), check objects (HEAD /v1/objects) and
publish a snapshot (POST /v1/build-input-snapshots), as the Mac's source
publisher does (thoughts-desktop tools/publisher publishcommand.cpp).
HTML pages (html_pages.py) read and change a folder's page list
(GET/POST /v1/html-pages). A card move onto a pending proposal's text is recorded
by the server (POST/GET /v1/review-card-pending-moves, card_pending_moves.py).
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "thoughts-cli/1"
TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"


class ApiError(Exception):
    """The server answered with an HTTP error status."""

    def __init__(self, status: int, body: bytes, method: str, path: str):
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        try:
            self.json = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.json = None
        super().__init__(f"{method} {path}: HTTP {status}: {self.detail}")

    @property
    def detail(self) -> str:
        if isinstance(self.json, dict):
            detail = self.json.get("detail")
            if isinstance(detail, str):
                return detail
            if detail is not None:
                return json.dumps(detail, ensure_ascii=False)
        return self.body.decode("utf-8", "replace")[:2000]

    @property
    def error_code(self) -> str | None:
        code = self.json.get("error_code") if isinstance(self.json, dict) else None
        return code if isinstance(code, str) else None


class TransportError(Exception):
    """No HTTP answer (connection refused, timeout, closed connection)."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ServerApi:
    def __init__(self, base_url: str, token: str, *, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Like the Mac's publisher: never through a proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, *, payload: Any = None, content: bytes | None = None,
                headers: dict[str, str] | None = None,
                response_headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        """One request; ``response_headers``, when given, receives the answer's headers (lowercase names)."""
        outgoing = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                    "User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        body = content
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        outgoing.update(headers or {})
        request = urllib.request.Request(self.base_url + path, data=body, method=method, headers=outgoing)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response_headers is not None:
                    response_headers.update({name.lower(): value for name, value in response.headers.items()})
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, error.read(), method, path) from None
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise TransportError(f"{method} {path}: {reason}") from None

    def json(self, method: str, path: str, payload: Any = None) -> Any:
        _status, body = self.request(method, path, payload=payload)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise TransportError(f"{method} {path}: the server returned invalid JSON") from None

    # ------------------------------------------------------------------ reads

    def sources(self) -> dict[str, Any]:
        """GET /v1/sources: {source_frontier, sources: [{path, revision, sha256, object_sha256, deleted}]}."""
        return self.json("GET", "/v1/sources")

    def source(self, path: str) -> dict[str, Any] | None:
        """GET /v1/sources/<path>, or None when the server has no such source."""
        try:
            return self.json("GET", "/v1/sources/" + urllib.parse.quote(path, safe="/"))
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def download(self, sha256: str) -> bytes:
        """GET /v1/objects/<sha256>, verified."""
        _status, data = self.request("GET", f"/v1/objects/{sha256}", headers={"Accept": "*/*"})
        if sha256_hex(data) != sha256:
            raise TransportError(f"object {sha256} failed SHA-256 verification")
        return data

    def device_sequence(self) -> dict[str, Any]:
        """GET /v1/device-sequence: {device_id, latest_device_sequence, next_device_sequence}."""
        return self.json("GET", "/v1/device-sequence")

    def health(self) -> dict[str, Any]:
        return self.json("GET", "/v1/health")

    def library_projects(self) -> dict[str, Any]:
        """GET /v1/library/projects: {projects: [{id, name, revision, project_configuration, documents, ...}]}."""
        return self.json("GET", "/v1/library/projects")

    def projections(self) -> dict[str, Any]:
        """GET /v1/projections: {projections: [{entity_type, entity_id, collection, revision, deleted, payload}]}."""
        return self.json("GET", "/v1/projections")

    def object_info(self, sha256: str) -> dict[str, Any] | None:
        """HEAD /v1/objects/<sha256>: {size_bytes, media_type} of an object the server holds, or None."""
        headers: dict[str, str] = {}
        try:
            self.request("HEAD", f"/v1/objects/{sha256}", headers={"Accept": "*/*"}, response_headers=headers)
        except ApiError as error:
            if error.status == 404:
                return None
            raise
        try:
            size = int(headers.get("content-length", ""))
        except ValueError:
            raise TransportError(f"HEAD /v1/objects/{sha256}: the server returned no object size") from None
        return {"size_bytes": size, "media_type": headers.get("content-type")}

    def html_pages(self, folder: str) -> dict[str, Any]:
        """GET /v1/html-pages?folder=<docs-x>: {pages: [...], unlisted_pages: [...]} (html_pages.py)."""
        return self.json("GET", "/v1/html-pages?" + urllib.parse.urlencode({"folder": folder}))

    def latest_build_inputs(self, target: str) -> dict[str, Any] | None:
        """GET /v1/build-input-snapshots/latest?target=<pdf>: the PDF's newest snapshot, or None."""
        try:
            return self.json("GET", "/v1/build-input-snapshots/latest?" + urllib.parse.urlencode({"target": target}))
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def render_status(self, target: str) -> dict[str, Any]:
        """GET /v1/render-status?target=<pdf>: {status, reason, source_frontier, build, publication}."""
        return self.json("GET", "/v1/render-status?" + urllib.parse.urlencode({"target": target}))

    def build(self, build_id: str) -> dict[str, Any]:
        """GET /v1/builds/<id>: the build record (status, error, result with failed_documents, warnings, log_sha256)."""
        return self.json("GET", "/v1/builds/" + urllib.parse.quote(build_id, safe=""))

    def build_source_diff(self, build_id: str) -> dict[str, Any]:
        """GET /v1/builds/<id>/source-diff: the failing documents' sources against their last good PDF."""
        return self.json("GET", "/v1/builds/" + urllib.parse.quote(build_id, safe="") + "/source-diff")

    # ------------------------------------------------------------------ writes

    def upload(self, content: bytes, media_type: str = TEXT_MEDIA_TYPE) -> str:
        """POST /v1/objects (idempotent: objects are content-addressed)."""
        digest = sha256_hex(content)
        _status, body = self.request("POST", "/v1/objects", content=content,
                                     headers={"Content-Type": media_type, "X-Content-SHA256": digest})
        try:
            answered = json.loads(body.decode("utf-8")).get("sha256")
        except (UnicodeDecodeError, ValueError, AttributeError):
            answered = None
        if answered != digest:
            raise TransportError("the server returned the wrong object identity")
        return digest

    def publish(self, publication: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/source-publications: one atomic manifest of source mutations."""
        return self.json("POST", "/v1/source-publications", publication)

    def operations(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /v1/operations: one atomic batch; answers {results: [{operation_id, outcome, entity_revision}]}.

        The server keeps every operation by its ID, so an identical retry is answered "duplicate"."""
        return self.json("POST", "/v1/operations", {"operations": operations})

    def library_change(self, change: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/library/projects: one Library-project action; answers with the new catalogue.

        The server keeps a receipt per operation_id, so an identical retry is answered without
        applying it twice; a stale base_revision is refused with 409."""
        return self.json("POST", "/v1/library/projects", change)

    def set_html_pages(self, change: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/html-pages: add, replace or remove pages of one folder, each naming its current object."""
        return self.json("POST", "/v1/html-pages", change)

    def publish_build_inputs(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/build-input-snapshots: register a PDF's build inputs (idempotent for identical content)."""
        return self.json("POST", "/v1/build-input-snapshots", snapshot)

    def merge_review_cards(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/review-card-merges: the server merges review cards in one transaction (review_card_merge_v1).

        The server keeps the receipt by request_id, so an identical retry answers the first result."""
        return self.json("POST", "/v1/review-card-merges", request)

    def create_pending_card_move(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/review-card-pending-moves: a card move that waits for a pending proposal
        (review_card_pending_moves_v1). The server keeps it by request_id, so an identical retry
        answers the first result."""
        return self.json("POST", "/v1/review-card-pending-moves", request)

    def pending_card_move(self, pending_move_id: str) -> dict[str, Any] | None:
        """GET /v1/review-card-pending-moves/<id>, or None when the server has no such move."""
        try:
            return self.json("GET", "/v1/review-card-pending-moves/" + urllib.parse.quote(pending_move_id, safe=""))
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def pending_card_moves(self, query: dict[str, Any]) -> dict[str, Any]:
        """GET /v1/review-card-pending-moves?card_id=&proposal_id=&state=: {pending_moves: [...]}."""
        suffix = urllib.parse.urlencode({key: value for key, value in query.items() if value not in (None, "")})
        return self.json("GET", "/v1/review-card-pending-moves" + (f"?{suffix}" if suffix else ""))

    def cancel_pending_card_move(self, pending_move_id: str) -> dict[str, Any]:
        """POST /v1/review-card-pending-moves/<id>/cancel: the card stays where it is."""
        return self.json("POST", "/v1/review-card-pending-moves/" + urllib.parse.quote(pending_move_id, safe="")
                         + "/cancel", {})

    def render_check(self, target: str = "all", request_id: str | None = None) -> dict[str, Any]:
        """POST /v1/render-checks: build what the new sources made stale, as the Mac does after a Sync."""
        payload: dict[str, Any] = {"target": target}
        if request_id:
            payload["request_id"] = request_id
        return self.json("POST", "/v1/render-checks", payload)
