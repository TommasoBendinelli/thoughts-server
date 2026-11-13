# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Durable Library project membership, independent of document format."""
from __future__ import annotations

import base64
import hashlib

from .models import SourceFile


def source_manifests(session, store, project: dict) -> list[dict]:
    """Deliver exact canonical rendering manifests with the project catalogue.

    These are required even when a PDF and its reader sources were cached.
    Source records and object hashes, rather than mutable disk files, are authoritative.
    """
    from .project_configuration import canonical_manifest_record
    source = canonical_manifest_record(session, project["id"])
    if source is not None:
        path = source.path
        raw = store.path(source.object_sha256).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.sha256:
            raise ValueError("Project configuration failed verification")
        return [{"path": path, "sha256": source.sha256, "content": base64.b64encode(raw).decode()}]
    raise ValueError("Missing current Project manifest: " + project["id"])
