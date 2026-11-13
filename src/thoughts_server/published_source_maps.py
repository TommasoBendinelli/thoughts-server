# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Read immutable source maps by exact PDF identity, shared by linking consumers."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import select

from .models import BuildJob, Publication


class LinkUnresolved(ValueError):
    """A selection cannot safely identify a complete source passage."""


def verified(store, identity):
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise LinkUnresolved("The exact source evidence is unavailable")
    try:
        raw = store.path(identity).read_bytes()
    except FileNotFoundError as exc:
        raise LinkUnresolved("The exact source evidence is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != identity:
        raise LinkUnresolved("Source evidence failed verification")
    return raw


def exact_manifest_record(session, store, filename, pdf_sha):
    publication = session.get(Publication, filename)
    identities = []
    if publication and publication.object_sha256 == pdf_sha:
        if not publication.project and not publication.source_artifact_sha256:
            return None, None
        identity = publication.source_artifact_sha256
        if identity:
            manifest = json.loads(verified(store, identity))
            if manifest.get("pdf", {}).get("pdf_sha256") == pdf_sha:
                return identity, manifest

    # A phone can upload after a newer build. Never map old coordinates on it.
    def visit(value):
        if isinstance(value, dict):
            if value.get("filename") == filename and value.get("sha256") == pdf_sha:
                identities.append(value.get("source_artifact_sha256"))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for job in session.scalars(
        select(BuildJob)
        .where(BuildJob.target.in_((filename, "all")), BuildJob.status.in_(("succeeded", "attention")))
        .order_by(BuildJob.created_at.desc())
    ):
        visit(job.result)
    for identity in dict.fromkeys(identities):
        if identity:
            manifest = json.loads(verified(store, identity))
            if manifest.get("pdf", {}).get("pdf_sha256") == pdf_sha:
                return identity, manifest
    raise LinkUnresolved("The saved PDF's source snapshot is unavailable")


def exact_manifest(session, store, filename, pdf_sha):
    return exact_manifest_record(session, store, filename, pdf_sha)[1]


@contextmanager
def exact_source_bundle(session_factory, store, filename, pdf_sha):
    """Expose the saved manifest through the native resolver's existing bundle format.

    Objects and historical build records remain in the durable object store;
    only this per-attempt view is temporary and no current pointer is changed.
    """
    with session_factory() as session:
        identity, manifest = exact_manifest_record(session, store, filename, pdf_sha)
    if manifest is None:
        raise LinkUnresolved("The annotated PDF has no source map")
    if (
        manifest.get("kind") != "thoughts-document-source-artifact"
        or manifest.get("pdf", {}).get("filename") != filename
    ):
        raise LinkUnresolved("The saved source map has an invalid document identity")
    identities = {identity}
    for source in manifest.get("sources", []):
        identities.add(source.get("object"))
    for segment in manifest.get("pdf", {}).get("segments", []):
        for field in ("lookup_object", "synctex_object", "renderer_evidence_object"):
            if segment.get(field):
                identities.add(segment[field])
    with tempfile.TemporaryDirectory(prefix="thoughts-exact-source-map-") as temporary:
        root = Path(temporary)
        (root / "objects").mkdir()
        (root / "documents").mkdir()
        for object_id in identities:
            verified(store, object_id)
            (root / "objects" / object_id).symlink_to(store.path(object_id).resolve())
        pointer = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact-pointer",
            "filename": filename,
            "pdf_sha256": pdf_sha,
            "artifact_sha256": identity,
        }
        (root / "documents" / (filename + ".json")).write_text(json.dumps(pointer))
        yield root
