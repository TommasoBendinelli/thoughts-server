# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A small in-memory stand-in for the Thoughts server's public sync endpoints.

It follows thoughts_server.sources.publish_sources where the client depends on
it: a publication is recorded by publication_id and answered again from that
record, a device sequence owned by another operation is refused with
device_sequence_assigned, objects must be uploaded first, a mutation whose base
is current is "applied", and a stale base is merged by ``merge`` (default: a
conflict, which refuses the whole publication). The Library-project catalogue
and the actions create-document sends (include_source, rename_document,
set_document_published) follow
thoughts_server.library_projects._mutate: a receipt per operation_id answers an
identical retry, and a stale base_revision is refused with 409. Build-input
snapshots follow thoughts_server.build_inputs.register_build_input_snapshot:
the target must be a declared note's PDF of the snapshot's folder, every object
must be held with its size, no entry may be a live source, an imported PDF must
be one the Project selects with its SHA-256, and identical content is answered
with the existing snapshot. The real server is exercised by
tests/test_thoughts_cli.py in the server repository and the integration test.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any

from thoughts_cli.api import ApiError, ServerApi, TransportError

EMPTY = hashlib.sha256(b"").hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def output(folder: str, relative: str) -> str:
    """thoughts_server.source_owner.default_folder_output: hashed in the legacy ``documents`` spelling."""
    return "document_" + sha((folder.rsplit("/", 1)[0] + "/documents/" + relative).encode())[:20] + ".pdf"


class FakeServer:
    def __init__(self, token: str = "token", device_id: str = "agent-device"):
        self.token = token
        self.device_id = device_id
        self.objects: dict[str, bytes] = {EMPTY: b""}
        self.sources: dict[str, dict[str, Any]] = {}
        self.sequences: set[int] = set()
        self.publications: dict[str, tuple[str, dict[str, Any]]] = {}
        self.published: list[dict[str, Any]] = []
        self.render_checks: list[dict[str, Any]] = []
        self.downloads: list[str] = []
        self.frontier = 0
        # Hooks: before a publication is applied, and how a stale base merges.
        self.before_publish: Callable[[dict[str, Any]], None] | None = None
        self.after_publish: Callable[[dict[str, Any]], None] | None = None
        self.merge: Callable[[bytes, bytes, bytes], bytes | None] = lambda base, current, incoming: None
        self.on_upload: Callable[[bytes], None] | None = None
        # Library Projects: id -> {id, name, revision, folder, configured, documents, document_names}.
        self.projects: dict[str, dict[str, Any]] = {}
        self.project_receipts: dict[str, str] = {}
        self.library_changes: list[dict[str, Any]] = []
        # Called before a Library-project action is checked (after its receipt lookup).
        self.before_library_change: Callable[[dict[str, Any]], None] | None = None
        # Build-input snapshots by SHA-256, each target's latest, and the POSTs received.
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.latest_snapshots: dict[str, str] = {}
        self.snapshot_posts: list[dict[str, Any]] = []
        self.media_types: dict[str, str] = {}
        # The answer headers of the last request (HEAD /v1/objects).
        self.last_headers: dict[str, str] = {}
        # GET /v1/render-status answers by target, and GET /v1/builds/<id>/source-diff by build.
        self.render_status: dict[str, dict[str, Any]] = {}
        self.source_diffs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ state helpers

    def seed(self, path: str, text: str | bytes) -> dict[str, Any]:
        data = text.encode("utf-8") if isinstance(text, str) else text
        digest = sha(data)
        self.objects[digest] = data
        previous = self.sources.get(path)
        self.frontier += 1
        self.sources[path] = {"path": path, "revision": (previous["revision"] + 1) if previous else 1,
                              "sha256": digest, "object_sha256": digest, "deleted": False}
        return self.sources[path]

    def text(self, path: str) -> str:
        return self.objects[self.sources[path]["object_sha256"]].decode("utf-8")

    def delete(self, path: str) -> None:
        """A tombstone: the path keeps its revision (bumped), its content is gone."""
        previous = self.sources[path]
        self.frontier += 1
        self.sources[path] = {**previous, "revision": previous["revision"] + 1, "sha256": EMPTY,
                              "object_sha256": EMPTY, "deleted": True}

    def add_project(self, project_id: str, name: str, *, configured: bool = True,
                    folders: tuple[str, ...] = ()) -> dict[str, Any]:
        """A Project; ``configured=False`` lists it without a manifest or default-folder entry.
        ``folders`` are its connected docs-* source folders."""
        folder = ".thoughts/projects/" + sha(project_id.encode()) + "/default"
        self.projects[project_id] = {"id": project_id, "name": name, "revision": 0, "folder": folder,
                                     "folder_id": str(uuid.uuid5(uuid.NAMESPACE_URL, folder)),
                                     "configured": configured, "documents": [], "document_names": {}, "pdfs": [],
                                     "connected": list(folders)}
        return self.projects[project_id]

    def add_pdf(self, project_id: str, filename: str, content: bytes, *, included: bool = True) -> dict[str, Any]:
        """An imported PDF of the Project, at the root of its default folder (Library-project action import)."""
        digest = sha(content)
        self.objects[digest] = content
        self.media_types[digest] = "application/pdf"
        pdf = {"document_id": str(uuid.uuid4()), "filename": filename, "sha256": digest, "size_bytes": len(content),
               "included": included}
        self.projects[project_id]["pdfs"].append(pdf)
        return pdf

    def add_object(self, content: bytes, media_type: str = "application/octet-stream") -> str:
        self.objects[sha(content)] = content
        self.media_types[sha(content)] = media_type
        return sha(content)

    def target_of(self, project_id: str, relative: str) -> str:
        return output(self.projects[project_id]["folder"], relative)

    def put_snapshot(self, target: str, project: str, entries: list[dict[str, Any]],
                     policy: str = "source-closure-v2") -> dict[str, Any]:
        """A snapshot as the Mac publishes it, without the checks."""
        entries = sorted(entries, key=lambda entry: entry["path"])
        manifest = (json.dumps({"schema_version": 3, "target": target, "entries": entries, "project": project,
                                "discovery_policy": policy}, sort_keys=True, separators=(",", ":")) + "\n").encode()
        record = {"schema_version": 3, "sha256": sha(manifest), "target": target, "project": project,
                  "discovery_policy": policy, "manifest_object_sha256": sha(manifest), "entries": entries}
        self.snapshots.setdefault(record["sha256"], record)
        self.latest_snapshots[target] = record["sha256"]
        return record

    def declare(self, project_id: str, relative: str, *, included: bool = True) -> str:
        """Declare a document of the Project's default folder directly (as another device did)."""
        document_id = str(uuid.uuid4())
        self.projects[project_id]["documents"].append({"document_id": document_id, "path": relative,
                                                       "included": included})
        return document_id

    def catalogue(self) -> dict[str, Any]:
        entries = []
        for project in sorted(self.projects.values(), key=lambda item: item["name"].casefold()):
            folder, folder_id = project["folder"], project["folder_id"]
            listed = {True: [], False: []}
            for pdf in project["pdfs"]:
                listed[pdf["included"]].append({
                    "id": pdf["filename"], "document_id": pdf["document_id"], "filename": pdf["filename"],
                    "sha256": pdf["sha256"], "size_bytes": pdf["size_bytes"], "start_page": 1,
                    "name": pdf["filename"][:-4], "included": pdf["included"], "published": pdf["included"],
                    "imported": True, "attachment_filename": pdf["filename"],
                    "managed_path": folder + "/" + pdf["filename"]})
            for declaration in project["documents"]:
                source = folder + "/" + declaration["path"]
                listed[declaration["included"]].append({
                    "id": source, "document_id": declaration["document_id"],
                    "name": project["document_names"].get(declaration["document_id"],
                                                          PurePosixPath(declaration["path"]).stem.replace("_", " ")),
                    "filename": output(folder, declaration["path"]), "sha256": "", "size_bytes": -1,
                    "start_page": 0, "source_file": source, "source_project": folder,
                    "included": declaration["included"], "published": declaration["included"]})
            connected = project.get("connected", [])
            entry = {"id": project["id"], "name": project["name"], "revision": project["revision"],
                     "source_folders": [{"project": item, "folder": item[5:]} for item in connected],
                     "target": "", "documents": listed[True],
                     "unpublished_documents": listed[False], "sources": []}
            if project["configured"]:
                entry["sources"] = [{"id": folder, "name": "Default source folder", "kind": "local",
                                     "folder_id": folder_id, "files": []}]
                entry["project_configuration"] = {
                    "schema_version": 5, "kind": "thoughts-project", "id": project["id"], "name": project["name"],
                    "default_folder_id": folder_id, "source_folders": [{"folder_id": folder_id, "path": folder}] + [
                        {"folder_id": str(uuid.uuid5(uuid.NAMESPACE_URL, item)), "path": item} for item in connected],
                    "documents": [{"document_id": pdf["document_id"], "source": {"folder_id": folder_id,
                                                                                "path": pdf["filename"]},
                                   "format": "pdf", "included": pdf["included"]} for pdf in project["pdfs"]] + [
                                  {"document_id": declaration["document_id"],
                                   "source": {"folder_id": folder_id, "path": declaration["path"]},
                                   "format": "latex" if declaration["path"].endswith(".tex") else "markdown",
                                   "included": declaration["included"]} for declaration in project["documents"]],
                    "document_names": dict(project["document_names"]),
                    "rendering": {**{pdf["document_id"]: {"sha256": pdf["sha256"]} for pdf in project["pdfs"]},
                                  **{declaration["document_id"]: {"output": output(folder, declaration["path"])}
                                     for declaration in project["documents"]}}}
            entries.append(entry)
        return {"schema_version": 1, "projects": entries}

    # ------------------------------------------------------------------ requests

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
        headers = {key.lower(): value for key, value in headers.items()}
        self.last_headers = {}
        if headers.get("authorization") != f"Bearer {self.token}":
            return self._json(401, {"detail": "Invalid device token"})
        route = urllib.parse.urlsplit(path).path
        if method == "HEAD" and route.startswith("/v1/objects/"):
            digest = route.rsplit("/", 1)[1]
            if digest not in self.objects:
                return 404, b""
            self.last_headers = {"Content-Length": str(len(self.objects[digest])),
                                 "Content-Type": self.media_types.get(digest, "application/octet-stream")}
            return 200, b""
        if method == "GET" and route == "/v1/build-input-snapshots/latest":
            target = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("target", [""])[0]
            digest = self.latest_snapshots.get(target)
            return (self._json(200, self.snapshots[digest]) if digest
                    else self._json(404, {"detail": "Build input snapshot not found"}))
        if method == "POST" and route == "/v1/build-input-snapshots":
            return self._register_snapshot(json.loads(body or b"{}"))
        if method == "GET" and route == "/v1/sources":
            return self._json(200, {"source_frontier": self.frontier,
                                    "sources": [self.sources[key] for key in sorted(self.sources)]})
        if method == "GET" and route.startswith("/v1/sources/"):
            source = self.sources.get(urllib.parse.unquote(route[len("/v1/sources/"):]))
            return self._json(200, source) if source else self._json(404, {"detail": "Source not found"})
        if method == "GET" and route.startswith("/v1/objects/"):
            digest = route.rsplit("/", 1)[1]
            self.downloads.append(digest)
            if digest not in self.objects:
                return self._json(404, {"detail": "Object not found"})
            return 200, self.objects[digest]
        if method == "POST" and route == "/v1/objects":
            data = body or b""
            if headers.get("x-content-sha256") != sha(data):
                return self._json(422, {"detail": "object digest mismatch"})
            if self.on_upload:
                self.on_upload(data)
            self.objects[sha(data)] = data
            return self._json(200, {"sha256": sha(data), "size_bytes": len(data), "media_type": headers.get("content-type")})
        if method == "GET" and route == "/v1/device-sequence":
            latest = max(self.sequences, default=0)
            return self._json(200, {"device_id": self.device_id, "latest_device_sequence": latest,
                                    "next_device_sequence": latest + 1})
        if method == "POST" and route == "/v1/source-publications":
            return self._publish(json.loads(body or b"{}"))
        if method == "GET" and route == "/v1/library/projects":
            return self._json(200, self.catalogue())
        if method == "POST" and route == "/v1/library/projects":
            return self._library_change(json.loads(body or b"{}"))
        if method == "POST" and route == "/v1/render-checks":
            self.render_checks.append(json.loads(body or b"{}"))
            return self._json(200, {"status": "building", "library_revision": 1, "build": {"id": "build-1"},
                                    "documents": []})
        if method == "GET" and route == "/v1/render-status":
            target = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("target", [""])[0]
            status = self.render_status.get(target)
            return (self._json(200, status) if status
                    else self._json(200, {"status": "synced", "reason": "The PDF matches.", "source_frontier": 1,
                                          "build": None, "publication": None}))
        if method == "GET" and route.startswith("/v1/builds/") and route.endswith("/source-diff"):
            diff = self.source_diffs.get(route.split("/")[3])
            return self._json(200, diff) if diff else self._json(422, {"detail": "no comparison"})
        if method == "GET" and route == "/v1/health":
            return self._json(200, {"status": "ok", "capabilities": []})
        return self._json(404, {"detail": "Not Found"})

    def _publish(self, publication: dict[str, Any]) -> tuple[int, bytes]:
        identity = sha(json.dumps(publication, sort_keys=True, separators=(",", ":")).encode())
        recorded = self.publications.get(publication["publication_id"])
        if recorded is not None:
            if recorded[0] != identity:
                return self._json(409, {"detail": "publication ID was reused with different source content"})
            return self._json(200, recorded[1])
        if self.before_publish:
            self.before_publish(publication)
        results = []
        staged = dict(self.sources)
        for mutation in publication["mutations"]:
            if mutation["device_sequence"] in self.sequences:
                latest = max(self.sequences)
                return self._json(409, {"detail": "device sequence is already assigned",
                                        "error_code": "device_sequence_assigned",
                                        "device_sequence": mutation["device_sequence"],
                                        "latest_device_sequence": latest, "next_device_sequence": latest + 1})
            if mutation["base_object_sha256"] not in self.objects or mutation["content_sha256"] not in self.objects:
                return self._json(409, {"detail": "base and incoming content objects must be uploaded first"})
            current = staged.get(mutation["path"])
            incoming = self.objects[mutation["content_sha256"]]
            if current and current["revision"] == mutation["base_revision"] and current["sha256"] == mutation["base_sha256"]:
                merged, outcome = incoming, "applied"
            elif current is None and mutation["base_revision"] == 0 and mutation["base_sha256"] == EMPTY:
                merged, outcome = incoming, "applied"  # a new source
            else:
                merged = self.merge(self.objects[mutation["base_object_sha256"]],
                                    self.objects[current["object_sha256"]] if current else b"", incoming)
                if merged is None:
                    return self._json(409, {"detail": f"source publication conflicts at {mutation['path']}; "
                                                      "no source was published"})
                outcome = "merged"
            digest = sha(merged)
            self.objects[digest] = merged
            staged[mutation["path"]] = {"path": mutation["path"], "revision": (current["revision"] if current else 0) + 1,
                                        "sha256": digest, "object_sha256": digest, "deleted": False}
            results.append({"outcome": outcome, "path": mutation["path"], "revision": staged[mutation["path"]]["revision"],
                            "sha256": digest, "object_sha256": digest, "server_sequence": self.frontier + 1})
        for mutation in publication["mutations"]:
            self.sequences.add(mutation["device_sequence"])
        self.sources = staged
        self.frontier += 1
        # Decision claims (protocol/agent-proposals.md 10.4.3): matched when the wrapper is gone afterwards.
        claims = publication.get("proposal_decisions")
        decisions = None if claims is None else [
            {"decision_id": claim["decision_id"], "proposal_id": claim["proposal_id"],
             "outcome": "unmatched" if claim["proposal_id"].encode() in self.objects[
                 staged[claim["source_file"]]["object_sha256"]] else "matched"}
            for claim in claims]
        answer = {"publication_id": publication["publication_id"], "trace_id": None,
                  "source_frontier": self.frontier, "results": results, "proposal_decisions": decisions}
        self.publications[publication["publication_id"]] = (identity, answer)
        self.published.append(publication)
        if self.after_publish:
            self.after_publish(publication)
        return self._json(200, answer)

    def _register_snapshot(self, request: dict[str, Any]) -> tuple[int, bytes]:
        self.snapshot_posts.append(request)
        if request.get("schema_version") != 3 or request.get("discovery_policy") not in {"tex-closure-v1",
                                                                                         "source-closure-v2"}:
            return self._json(422, {"detail": "invalid build input snapshot"})
        target, folder = request.get("target"), request.get("project")
        project = next((item for item in self.projects.values() if item["folder"] == folder), None)
        # Published or not: the server registers a declared note's inputs either way.
        declared = project is not None and any(
            self.target_of(project["id"], declaration["path"]) == target for declaration in project["documents"])
        if not declared:
            return self._json(409, {"detail": "build input snapshot does not match the Library source project"})
        selected = {folder + "/" + pdf["filename"]: pdf["sha256"] for pdf in project["pdfs"] if pdf["included"]}
        for entry in request["entries"]:
            path = entry["path"]
            if entry["role"] in {"asset", "generated"} and not path.startswith(folder + "/"):
                return self._json(409, {"detail": f"build input escapes its source folder: {path}"})
            if path.endswith(".pdf") and path.startswith(".thoughts/") and selected.get(path) != entry["sha256"]:
                return self._json(409, {"detail": "Managed PDF input does not match this Project's selected document"})
            if entry["sha256"] not in self.objects:
                return self._json(409, {"detail": f"build input object is unavailable: {path}"})
            if len(self.objects[entry["sha256"]]) != entry["size_bytes"]:
                return self._json(409, {"detail": f"build input size does not match its object: {path}"})
            if path in self.sources and not self.sources[path]["deleted"]:
                return self._json(409, {"detail": f"build input overlaps canonical source: {path}"})
        record = self.put_snapshot(target, folder, request["entries"], request["discovery_policy"])
        return self._json(200, record)

    def _library_change(self, request: dict[str, Any]) -> tuple[int, bytes]:
        identity = sha(json.dumps(request, sort_keys=True, separators=(",", ":")).encode())
        receipt = self.project_receipts.get(request.get("operation_id"))
        if receipt is not None:
            if receipt != identity:
                return self._json(409, {"detail": "Project operation ID was reused with different content"})
            return self._json(200, self.catalogue())
        if self.before_library_change:
            self.before_library_change(request)
        project = self.projects.get(request.get("project_id"))
        if project is None:
            return self._json(409, {"detail": "This project is no longer in the Library"})
        if int(request.get("base_revision", 0)) != project["revision"]:
            return self._json(409, {"detail": "This project changed on another device. Sync and try again"})
        action = request.get("action")
        if action == "include_source":
            source, prefix = request.get("source_file", ""), project["folder"] + "/"
            relative = source[len(prefix):]
            current = self.sources.get(source)
            if (not source.startswith(prefix) or current is None or current["deleted"]
                    or PurePosixPath(relative).suffix.lower() not in {".tex", ".md", ".markdown"}):
                return self._json(409, {"detail": "Choose a Markdown or LaTeX document in this project's folder"})
            text = re.sub(r"(?<!\\)%.*", "", self.text(source))
            if relative.endswith(".tex") and not (re.search(r"(?m)^\s*\\documentclass\b", text)
                                                  and re.search(r"\\begin\s*\{document\}", text)):
                return self._json(409, {"detail": "Choose a complete LaTeX document with a document class"})
            if not any(declaration["path"] == relative for declaration in project["documents"]):
                project["documents"].append({"document_id": request.get("new_document_id") or str(uuid.uuid4()),
                                             "path": relative, "included": True})
        elif action == "rename_document":
            declaration = next((item for item in project["documents"] if request.get("document_id")
                                in {item["document_id"], project["folder"] + "/" + item["path"]}), None)
            if declaration is None:
                return self._json(409, {"detail": "The document is no longer in this project"})
            name = request.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                return self._json(409, {"detail": "Enter a project name between 1 and 200 characters without line breaks"})
            project["document_names"][declaration["document_id"]] = name.strip()
        elif action == "move_document":
            # thoughts_server.document_moves.move_document: an imported PDF to another Project's default folder.
            destination = self.projects.get(request.get("destination_project_id"))
            if destination is None or destination is project:
                return self._json(409, {"detail": "Choose a different destination project"})
            if int(request.get("destination_revision", -1)) != destination["revision"]:
                return self._json(409, {"detail": "The destination project changed on another device. Sync and try again"})
            pdf = next((item for item in project["pdfs"] if item["document_id"] == request.get("document_id")), None)
            if pdf is None:
                return self._json(409, {"detail": "Choose an unlinked PDF in this project"})
            project["pdfs"].remove(pdf)
            destination["pdfs"].append(pdf)
            destination["revision"] += 1
        elif action == "set_document_published":
            declaration = next((item for item in project["documents"]
                                if request.get("document_id") == item["document_id"]), None)
            if declaration is None:
                return self._json(409, {"detail": "The document is no longer in this project"})
            if type(request.get("published")) is not bool:
                return self._json(409, {"detail": "Say whether the document is published"})
            declaration["included"] = request["published"]
        else:
            return self._json(409, {"detail": "Unsupported project action"})
        project["revision"] += 1
        self.project_receipts[request["operation_id"]] = identity
        self.library_changes.append(request)
        return self._json(200, self.catalogue())

    @staticmethod
    def _json(status: int, value: Any) -> tuple[int, bytes]:
        return status, json.dumps(value).encode()


class FakeApi(ServerApi):
    """ServerApi routed straight to a FakeServer; ``fail`` raises TransportError for matching calls."""

    def __init__(self, server: FakeServer):
        super().__init__("http://fake", server.token)
        self.server = server
        self.calls: list[tuple[str, str]] = []
        self.lose_answer: Callable[[str, str], bool] = lambda method, path: False

    def request(self, method, path, *, payload=None, content=None, headers=None, response_headers=None):
        self.calls.append((method, path))
        body = content
        outgoing = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode()
        status, answer = self.server.handle(method, path, outgoing, body)
        if response_headers is not None:
            response_headers.update({name.lower(): value for name, value in self.server.last_headers.items()})
        if self.lose_answer(method, path):
            raise TransportError(f"{method} {path}: Remote end closed connection without response")
        if status >= 400:
            raise ApiError(status, answer, method, path)
        return status, answer


class HttpFake:
    """The FakeServer on a loopback port, for the CLI's real HTTP transport."""

    def __init__(self, server: FakeServer):
        fake = server

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                status, answer = fake.handle(method, self.path, dict(self.headers.items()), body)
                extra = dict(fake.last_headers)
                self.send_response(status)
                self.send_header("Content-Length", extra.pop("Content-Length", str(len(answer))))
                for name, value in extra.items():
                    self.send_header(name, value)
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(answer)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_HEAD(self):  # noqa: N802
                self._serve("HEAD")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> HttpFake:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
