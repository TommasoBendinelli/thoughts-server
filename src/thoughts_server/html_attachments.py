# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Opaque HTML objects; only this small reference manifest is authored source."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
from pathlib import Path
from .source_resources import (
    SourceResourceError,
    normalize_manifest_resources,
    source_resource_kind,
    validate_html_file,
)

MAX_HTML_BYTES = 100_000_000
MANIFEST = "tex/support/thoughts_html_attachments.txt"


def is_attachment_path(path: object) -> bool:
    """A logical path a ``thoughts-html:`` link can name (the source closure's ``html_attachment`` rule).

    Imported attachments live at ``docs-<folder>/tex/support/html/<file>.html``; HTML files
    in a connected folder's source tree (``tex/`` or ``markdown/``) are attachments too.
    """
    if not isinstance(path, str):
        return False
    try:
        return source_resource_kind(path, path.partition("/")[0]) == "html_attachment"
    except SourceResourceError:
        return False


def attachments(project_root: Path) -> list[dict]:
    path = project_root / MANIFEST
    if not path.exists():
        return []
    if path.stat().st_size > 2_000_000:
        raise ValueError("HTML attachment manifest is too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Invalid HTML attachment manifest")
    return normalize_manifest_resources(value.get("attachments"), project_root.name)

def add(session, store, root, operation_id, project, request):
    from .models import ObjectRecord
    from .library_projects import _save_source
    digest = request.get("sha256")
    record = session.get(ObjectRecord, digest) if isinstance(digest, str) else None
    if record is None:
        raise ValueError("HTML attachment object is unavailable")
    resource = {"kind": "html_attachment", "path": project + "/tex/support/html/" + request.get("filename", ""),
                "sha256": digest, "object": digest, "bytes": record.size_bytes}
    normalize_manifest_resources([resource], project)
    if record.size_bytes < 1 or not store.matches(digest, record.size_bytes):
        raise ValueError("HTML attachment object failed verification")
    validate_html_file(store.path(digest))
    entries = attachments(root / project)
    for entry in entries:
        if entry["path"].casefold() == resource["path"].casefold():
            if entry != resource:
                raise ValueError("A different attachment already uses this name")
            return
    entries.append(resource)
    _save_source(session, store, root, operation_id, project + "/" + MANIFEST,
                 (json.dumps({"schema_version": 1, "attachments": entries}, sort_keys=True) + "\n").encode())

def materialize(store, workspace, project):
    """Resolve canonical blobs only into the disposable rendering workspace.

    The folder's page list is the authority for its pages (protocol/html-pages.md 1): a listed
    page replaces a copy of the same path that a PDF's build inputs brought into the workspace,
    so a page changed on Fedora is in the next build even while a Mac still uploads its old copy.
    """
    for entry in attachments(workspace / project):
        if not store.matches(entry["object"], entry["bytes"]):
            raise ValueError("HTML attachment object failed verification: " + entry["path"])
        destination = workspace / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.resolve().is_relative_to(workspace.resolve()) or destination.is_symlink():
            raise ValueError("HTML attachment path escapes workspace")
        # A new file, renamed over any build-input copy: never written into an existing inode.
        staged = destination.with_name("." + destination.name + ".thoughts-page")
        with store.open(entry["object"]) as source, staged.open("wb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
        os.replace(staged, destination)
