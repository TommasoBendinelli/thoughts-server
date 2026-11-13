# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Import a PDF from a URL into a Library Project (protocol/library-url-imports.md).

An agent that has only the remote connector has no file to upload, so the
server downloads the PDF itself and then makes exactly the changes the Mac's
Import document makes: the PDF becomes a standalone Library PDF (as
``POST /v1/library/imports`` does) and the Project's ``import`` change adds it
(``library_projects.mutate``). The Mac and the phone pick it up on their next
Sync like any other import.

The download is the only outbound request the server makes on behalf of a
client, so it is narrow: https on port 443 only, every address the host
resolves to must be a public one (no loopback, private, link-local, shared or
reserved range), the connection goes to the checked address (a second DNS
answer cannot redirect it), each redirect is checked the same way, and the
whole download has a deadline and a size cap.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import os
import re
import socket
import ssl
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, urljoin, urlsplit

from sqlalchemy import select

CAPABILITY = "library_import_pdf_v1"
MAX_PDF_BYTES = 100 * 1024 * 1024
DOWNLOAD_SECONDS = 120.0
SOCKET_SECONDS = 30.0
MAX_REDIRECTS = 5
NAME_LIMIT = 150
URL_LIMIT = 4096
PROJECT_LIMIT = 2000
# Some publishers refuse clients that do not look like a browser; the Mac's import_pdf sends the same.
USER_AGENT = "Mozilla/5.0 (Thoughts import_pdf)"
REDIRECTS = {301, 302, 303, 307, 308}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UrlImportError(ValueError):
    """A refused import, with the error code and HTTP status the API answers with."""

    def __init__(self, error_code: str, message: str, status: int = 422):
        super().__init__(message)
        self.error_code = error_code
        self.status = status


def refused_address(address: str) -> bool:
    """Whether a connection to this address could reach something that is not the public internet."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        # An IPv6 spelling of an IPv4 address is judged as that address.
        embedded = (
            ip.ipv4_mapped
            or ip.sixtofour
            or (ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip in _NAT64 else None)
        )
        if ip.teredo is not None:
            return True
        if embedded is not None:
            ip = embedded
    return (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def checked_url(url: str) -> tuple[str, int, str]:
    """(host, port, request target) of an https URL the server may download, or UrlImportError."""
    if not isinstance(url, str) or not url.strip() or len(url) > URL_LIMIT:
        raise UrlImportError("invalid_url", f"Give an https URL of at most {URL_LIMIT} characters.")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as error:
        raise UrlImportError("invalid_url", f"The URL is not valid: {error}.") from error
    if parts.scheme.lower() != "https":
        raise UrlImportError("invalid_url", "Only https URLs can be imported.")
    if not parts.hostname:
        raise UrlImportError("invalid_url", "The URL has no host.")
    if parts.username is not None or parts.password is not None:
        raise UrlImportError("invalid_url", "The URL must not contain a user name or password.")
    if port not in (None, 443):
        raise UrlImportError("invalid_url", "Only https URLs on the standard port (443) can be imported.")
    if any(ord(c) < 32 or ord(c) == 127 for c in url.strip()):
        raise UrlImportError("invalid_url", "The URL contains control characters.")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    # As a browser sends it: spaces and other characters outside ASCII percent-encoded, the rest kept.
    return parts.hostname, 443, quote(target, safe="!#$%&'()*+,/:;=?@[]~")


def _resolve(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def checked_address(host: str, port: int, resolve: Callable[[str, int], list[str]] | None = None) -> str:
    """The address to connect to: refused when any address the host has is not public."""
    resolve = resolve or _resolve
    try:
        addresses = list(dict.fromkeys(resolve(host, port)))
    except (OSError, UnicodeError) as error:
        raise UrlImportError("download_failed", f"The host {host} could not be found.", 502) from error
    if not addresses:
        raise UrlImportError("download_failed", f"The host {host} could not be found.", 502)
    if any(refused_address(address) for address in addresses):
        raise UrlImportError(
            "address_refused",
            f"The host {host} is a loopback, private or link-local address; "
            "only PDFs on the public internet can be imported.",
        )
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to one checked address; the URL's host names the server for TLS (SNI, certificate) and Host."""

    def __init__(self, host: str, port: int, address: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._tls = context

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class _Response:
    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse):
        self._connection, self._response = connection, response
        self.status = response.status

    def getheader(self, name: str) -> str | None:
        return self._response.getheader(name)

    def read(self, size: int) -> bytes:
        # read1 returns what arrived, so the deadline is checked while a slow server trickles.
        return self._response.read1(size)

    def close(self) -> None:
        self._response.close()
        self._connection.close()


def _open(host: str, port: int, target: str, address: str, timeout: float) -> _Response:
    connection = _PinnedHTTPSConnection(host, port, address, timeout, ssl.create_default_context())
    try:
        connection.request(
            "GET",
            target,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,*/*;q=0.8",
                "Accept-Encoding": "identity",
            },
        )
        return _Response(connection, connection.getresponse())
    except BaseException:
        connection.close()
        raise


def fetch_pdf(
    url: str,
    output: BinaryIO,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    seconds: float = DOWNLOAD_SECONDS,
    resolve: Callable[[str, int], list[str]] | None = None,
    open_url: Callable | None = None,
) -> int:
    """Download the PDF at @p url into @p output and return its size; UrlImportError when refused."""
    resolve, open_url = resolve or _resolve, open_url or _open
    deadline = time.monotonic() + seconds
    current = url.strip() if isinstance(url, str) else url
    for _ in range(MAX_REDIRECTS + 1):
        host, port, target = checked_url(current)
        address = checked_address(host, port, resolve)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UrlImportError(
                "download_failed", f"The download did not finish within {int(seconds)} seconds.", 502
            )
        try:
            response = open_url(host, port, target, address, min(SOCKET_SECONDS, remaining))
        except (OSError, http.client.HTTPException, UnicodeError) as error:
            raise UrlImportError(
                "download_failed", f"The download from {host} failed: {error}", 502
            ) from error
        try:
            if response.status in REDIRECTS:
                location = response.getheader("Location")
                if not location or not location.strip():
                    raise UrlImportError("download_failed", f"{host} redirected without a location.", 502)
                current = urljoin(current, location.strip())
                continue
            if response.status != 200:
                raise UrlImportError("download_failed", f"{host} answered HTTP {response.status}.", 502)
            declared = response.getheader("Content-Length")
            if declared and declared.strip().isdigit() and int(declared) > max_bytes:
                raise UrlImportError(
                    "too_large", f"The PDF is larger than {max_bytes // (1024 * 1024)} MiB.", 413
                )
            return _copy(response, output, max_bytes, deadline, seconds, host)
        except (OSError, http.client.HTTPException) as error:
            raise UrlImportError(
                "download_failed", f"The download from {host} failed: {error}", 502
            ) from error
        finally:
            response.close()
    raise UrlImportError("download_failed", f"The URL redirected more than {MAX_REDIRECTS} times.", 502)


def _copy(response, output: BinaryIO, max_bytes: int, deadline: float, seconds: float, host: str) -> int:
    size, head = 0, b""
    while chunk := response.read(256 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise UrlImportError(
                "too_large", f"The PDF is larger than {max_bytes // (1024 * 1024)} MiB.", 413
            )
        if len(head) < 5:
            head += chunk[: 5 - len(head)]
            if len(head) == 5 and head != b"%PDF-":
                raise UrlImportError(
                    "not_pdf", "The URL did not return a PDF (for arXiv use the /pdf/ link)."
                )
        output.write(chunk)
        if time.monotonic() > deadline:
            raise UrlImportError(
                "download_failed",
                f"The download from {host} did not finish within {int(seconds)} seconds.",
                502,
            )
    if head != b"%PDF-":
        raise UrlImportError("not_pdf", "The URL did not return a PDF (for arXiv use the /pdf/ link).")
    return size


def checked_name(name) -> str:
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > NAME_LIMIT
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise UrlImportError(
            "invalid_name", f"Give a document name of 1 to {NAME_LIMIT} characters without line breaks."
        )
    return name.strip()


def library_filename(name: str, sha256: str, project_id: str) -> str:
    """The Library filename the Mac's Import document gives this PDF (LibraryProjectStore::importPdf)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    if not safe or not safe[0].isalnum():
        safe = "document_" + safe
    project_key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    return f"{safe}_{sha256[:12]}_{project_key[:8]}.pdf"


def find_project(projects: list[dict], value) -> dict:
    """A catalogue project by its id, else by its name ignoring case."""
    if not isinstance(value, str) or not value.strip() or len(value) > PROJECT_LIMIT:
        raise UrlImportError("project_not_found", "Give the project's name or id.", 404)
    exact = next((p for p in projects if p["id"] == value), None)
    if exact is not None:
        return exact
    named = [p for p in projects if p["name"].casefold() == value.strip().casefold()]
    if len(named) == 1:
        return named[0]
    if named:
        raise UrlImportError(
            "project_ambiguous",
            f"Several projects are named {value.strip()!r}; give its id: "
            + ", ".join(p["id"] for p in named),
            409,
        )
    names = sorted((p["name"] for p in projects), key=str.casefold)
    raise UrlImportError(
        "project_not_found",
        f"No Library project is named {value.strip()!r}. Projects: "
        + ("; ".join(names[:60]) + (" …" if len(names) > 60 else "") if names else "none"),
        404,
    )


def _documents(project: dict) -> list[dict]:
    return list(project.get("documents", [])) + list(project.get("unpublished_documents", []))


def _answer(project: dict, document: dict, record, *, replayed: bool, url: str, page_count: int) -> dict:
    return {
        "accepted": True,
        "replayed": replayed,
        "project_id": project["id"],
        "project": project["name"],
        "document_id": document.get("document_id"),
        "filename": document["filename"],
        "name": document.get("name"),
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "page_count": page_count,
        "source_url": url,
    }


def _write_new_file(destination: Path, store, sha256: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output, store.open(sha256) as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _stage_standalone(session, store, root: Path, filename: str, record, url: str) -> list[Path]:
    """Make the PDF a standalone Library PDF, as POST /v1/library/imports does; uncommitted.

    Returns the files this call created, for removal if the Project change fails."""
    from .document_identity import register_document
    from .models import DocumentIdentity, Publication, new_document_id

    publication = session.get(Publication, filename)
    if publication is not None:
        metadata = publication.metadata_json or {}
        if (
            metadata.get("kind") != "unlinked"
            or publication.object_sha256 != record.sha256
            or publication.project
        ):
            raise UrlImportError(
                "filename_taken", f"A different Library PDF already uses the filename {filename}.", 409
            )
        document_id = publication.document_id
    else:
        identity = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
        document_id = identity.document_id if identity is not None else new_document_id()
    register_document(session, document_id, filename)
    created = []
    destination = root / "current_pdfs" / filename
    if destination.is_file():
        if not store._matches(destination, record.sha256, record.size_bytes):
            raise UrlImportError(
                "filename_taken", f"A different Library PDF already uses the filename {filename}.", 409
            )
    else:
        _write_new_file(destination, store, record.sha256)
        created.append(destination)
    if publication is None:
        publication = Publication(
            filename=filename,
            document_id=document_id,
            object_sha256=record.sha256,
            size_bytes=record.size_bytes,
            project="",
            source_frontier=0,
            comment_frontier=0,
            annotation_frontier=0,
            metadata_json={
                "filename": filename,
                "pdf_sha256": record.sha256,
                "kind": "unlinked",
                "archived": False,
            },
        )
        session.add(publication)
    # The document's source link: the URL it came from, as the Mac keeps it for a link import.
    publication.metadata_json = {**(publication.metadata_json or {}), "source_url": url}
    session.flush()
    return created


def import_pdf(
    session,
    store,
    root: Path,
    request: dict,
    *,
    settings=None,
    device_id: str | None = None,
    lock=None,
    fetch: Callable | None = None,
) -> dict:
    """Download request["url"] and import it into request["project"] as request["name"].

    @p lock is the worktree lock's context manager factory; the download runs before it is taken."""
    from contextlib import nullcontext

    from .library_projects import TYPE, _pdf_page_count, _record, catalogue, mutate
    from .project_configuration import canonical_for_project, configured_default_folder, default_folder

    if not isinstance(request, dict):
        raise UrlImportError("invalid_request", "Give project, name and url.")
    name = checked_name(request.get("name"))
    url = request.get("url")
    checked_url(url)
    url = url.strip()
    # A misspelt project fails before anything is downloaded.
    find_project(catalogue(session, root, store, sources=False)["projects"], request.get("project"))
    session.rollback()  # no transaction stays open during the download

    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as staged:
        (fetch or fetch_pdf)(url, staged)
        staged.seek(0)
        record = store.put_stream(session, staged, "application/pdf")
    session.commit()
    try:
        page_count = _pdf_page_count(store, record.sha256, record.size_bytes)
    except ValueError as error:
        raise UrlImportError("invalid_pdf", f"The URL returned a PDF that cannot be read: {error}") from error

    with lock() if lock is not None else nullcontext():
        project = find_project(
            catalogue(session, root, store, sources=False)["projects"], request.get("project")
        )
        existing = next((d for d in _documents(project) if d.get("sha256") == record.sha256), None)
        if existing is not None:
            session.rollback()
            return _answer(project, existing, record, replayed=True, url=url, page_count=page_count)
        key = project["id"]
        filename = library_filename(name, record.sha256, key)
        configuration = canonical_for_project(session, store, key)
        managed = (
            root
            / (configured_default_folder(configuration) if configuration is not None else default_folder(key))
            / filename
        )
        had_managed = managed.exists()
        created = _stage_standalone(session, store, root, filename, record, url)
        saved = _record(session, TYPE, key)
        change = {
            "operation_id": str(uuid.uuid4()),
            "project_id": key,
            "action": "import",
            "base_revision": saved.revision if saved is not None else 0,
            "filename": filename,
            "sha256": record.sha256,
            "name": name,
        }
        try:
            result = mutate(session, store, root, change, settings=settings, trace_owner_device_id=device_id)
        except Exception:
            session.rollback()
            for path in created + ([managed] if not had_managed else []):
                path.unlink(missing_ok=True)
            raise
    project = next(p for p in result["projects"] if p["id"] == key)
    document = next(
        (d for d in _documents(project) if d.get("filename") == filename),
        {"filename": filename, "name": name},
    )
    return _answer(project, document, record, replayed=False, url=url, page_count=page_count)
