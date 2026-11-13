# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Validated append-only PDF attachments for one source-backed Thoughts project."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

ATTACHMENT_MANIFEST = Path("tex/support/thoughts_project_attachments.txt")
_SAFE_PDF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProjectAttachmentError(ValueError):
    """Raised when a project attachment manifest is unsafe or inconsistent."""


@dataclass(frozen=True)
class ProjectAttachment:
    filename: str
    sha256: str
    title: str
    placement: str = "append"
    source: str | None = None
    document_id: str | None = None


def _valid_title(value: str) -> bool:
    return bool(value) and len(value) <= 200 and not any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )


def load_project_attachments(project_root: Path, *, include_disabled: bool = False) -> tuple[ProjectAttachment, ...]:
    """Load one project's deterministic attachment order."""

    from . import project_manifest
    try:
        value = project_manifest.attachments(project_manifest.load(project_root), include_disabled=include_disabled)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ProjectAttachmentError(f"project attachment manifest is unreadable: {exc}") from exc
    attachments = value.get("attachments") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(attachments, list)
    ):
        raise ProjectAttachmentError("project attachment manifest has an unsupported schema")

    parsed: list[ProjectAttachment] = []
    seen: set[str] = set()
    for raw in attachments:
        filename = raw.get("filename") if isinstance(raw, dict) else None
        digest = raw.get("sha256") if isinstance(raw, dict) else None
        title = raw.get("title") if isinstance(raw, dict) else None
        placement = raw.get("placement") if isinstance(raw, dict) else None
        if (
            not isinstance(filename, str)
            or not _SAFE_PDF.fullmatch(filename)
            or Path(filename).name != filename
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or not isinstance(title, str)
            or title != title.strip()
            or not _valid_title(title)
            or placement != "append"
        ):
            raise ProjectAttachmentError("project attachment manifest contains an invalid entry")
        folded = filename.casefold()
        if folded in seen:
            raise ProjectAttachmentError(
                f"project attachment manifest contains duplicate filename: {filename}"
            )
        seen.add(folded)
        source = raw.get("source")
        if source is not None:
            from .source_owner import folder_owner
            from .uniform_manifest import input_path
            input_path(folder_owner(project_root), source, "pdf")
        parsed.append(ProjectAttachment(filename, digest, title, placement, source, raw.get("document_id")))
    return tuple(parsed)


def project_attachment_pdfs(
    docs_root: Path, project_root: Path
) -> tuple[tuple[ProjectAttachment, Path], ...]:
    """Resolve and verify all external PDFs selected by a project attachment manifest."""

    docs_root = docs_root.resolve()
    resolved: list[tuple[ProjectAttachment, Path]] = []
    for attachment in load_project_attachments(project_root):
        path = (project_root / attachment.source).resolve() if attachment.source else docs_root / "third_party" / attachment.filename
        if not path.is_relative_to(docs_root):
            raise ProjectAttachmentError("Project PDF input escapes the documents root")
        if not path.is_file():
            raise ProjectAttachmentError(
                f"project attachment is unavailable: {attachment.filename}"
            )
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
        except OSError as exc:
            raise ProjectAttachmentError(
                f"project attachment could not be read: {attachment.filename}"
            ) from exc
        if actual != attachment.sha256:
            raise ProjectAttachmentError(
                f"project attachment hash mismatch: {attachment.filename}"
            )
        resolved.append((attachment, path.resolve()))
    return tuple(resolved)


def verified_attachment_object(store, filename: str, sha256: str, size_bytes: int) -> Path:
    """Resolve a canonical attachment by identity, independently of Library caches."""
    if not _SAFE_PDF.fullmatch(filename) or not _SHA256.fullmatch(sha256):
        raise ProjectAttachmentError("project attachment has an invalid filename or hash")
    if not store.matches(sha256, size_bytes):
        raise ProjectAttachmentError(f"project attachment object is unavailable or corrupt: {filename} ({sha256})")
    return store.path(sha256)


def materialize_workspace_attachments(session, store, workspace: Path, projects) -> int:
    """Prepare renderer path aliases only inside an isolated build workspace.

    The filenames remain stable for existing outlines, links and comment anchors;
    the manifest hash, never a permanent third_party file, owns the bytes.
    """
    from .models import ObjectRecord
    from .project_configuration import manifest_files, read

    # Full builds and previews can retain old docs-* folders. Only current
    # manifest declarations authorize reading their attachment configuration.
    try:
        configured = {folder["path"]
                      for manifest in manifest_files(workspace)
                      for folder in read(manifest)["source_folders"]}
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ProjectAttachmentError(f"current Project manifest is unreadable: {error}") from error

    changed = 0
    for project in sorted(set(projects)):
        from .source_owner import is_default_folder, is_owner
        if not is_owner(project):
            raise ProjectAttachmentError("invalid attachment project")
        if project not in configured:
            continue
        from .html_attachments import materialize
        # HTML attachments belong to connected docs-* folders only.
        if not is_default_folder(project):
            materialize(store, workspace, project)
        for attachment in load_project_attachments(workspace / project):
            obj = session.get(ObjectRecord, attachment.sha256)
            if obj is None:
                raise ProjectAttachmentError(f"{project}: project attachment object is unavailable: {attachment.filename} ({attachment.sha256})")
            try:
                # Full workspace copies can contain an obsolete reader alias.
                # Replace only this disposable copy, never its authoring original.
                destination = ((workspace / project / attachment.source).resolve() if attachment.source
                               else workspace / "third_party" / attachment.filename)
                directory = destination.parent
                if not directory.is_symlink() and (destination.exists() or destination.is_symlink()):
                    if destination.is_symlink() or not store._matches(destination, attachment.sha256, obj.size_bytes):
                        destination.unlink()
                changed += materialize_project_attachment(workspace, store, attachment.filename, attachment.sha256, obj.size_bytes,
                                                          destination=destination)
            except ProjectAttachmentError as error:
                raise ProjectAttachmentError(f"{project}: {error}") from error
    return changed


def materialize_project_attachment(docs_root, store, filename, sha256, size_bytes, *, destination=None):
    """Copy verified PDF bytes to an explicit managed input or a renderer alias."""
    if not _SAFE_PDF.fullmatch(filename) or not _SHA256.fullmatch(sha256):
        raise ProjectAttachmentError("project attachment has an invalid filename or hash")
    verified_attachment_object(store, filename, sha256, size_bytes)
    destination = destination or docs_root.resolve() / "third_party" / filename
    if not destination.resolve().is_relative_to(docs_root.resolve()):
        raise ProjectAttachmentError("Project attachment escapes the workspace")
    directory = destination.parent
    if directory.is_symlink() or destination.is_symlink():
        raise ProjectAttachmentError(f"project attachment destination is a symbolic link: {filename}")
    if destination.exists():
        if destination.is_file() and store._matches(destination, sha256, size_bytes):
            return False
        raise ProjectAttachmentError(f"a different file already uses the project attachment filename: {filename}")

    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".project-attachment-", dir=directory)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "wb") as output, store.open(sha256) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != sha256 or size != size_bytes:
            raise ProjectAttachmentError(f"project attachment object failed verification: {filename}")
        # Linking the complete temporary file installs it atomically without
        # overwriting a file created by another writer during the copy.
        os.link(temporary, destination)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except OSError as exc:
        raise ProjectAttachmentError(f"could not materialize project attachment {filename}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
