# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Refresh source-backed ordinary comments without rendering a PDF."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .comment_records import comment_record
from .library import store_published_source_index
from .models import Entity, Publication, SourceFile, utc_now
from .object_store import ObjectStore
from .review_correspondence import source_set_sha256, strict_gate_failures
from .review_metadata import metadata_refresh

COMMENT_ANCHOR_RE = re.compile(r"\\(?:CommentHighlight|CommentStrikethrough|CommentAnchor|ReviewAnchor)(?![A-Za-z@])")
MAX_COMMENT_ID_LENGTH = 512


def _skip_space(value: str, offset: int) -> int:
    while offset < len(value):
        if value[offset].isspace():
            offset += 1
            continue
        if value[offset] == "%" and (offset == 0 or value[offset - 1] != "\\"):
            newline = value.find("\n", offset)
            return len(value) if newline < 0 else _skip_space(value, newline + 1)
        return offset
    return offset


def _has_unescaped_percent(value: str) -> bool:
    """Return whether a TeX line contains a comment introducer."""

    for offset, character in enumerate(value):
        if character != "%":
            continue
        backslashes = 0
        previous = offset - 1
        while previous >= 0 and value[previous] == "\\":
            backslashes += 1
            previous -= 1
        if backslashes % 2 == 0:
            return True
    return False


def _braced_argument(value: str, offset: int) -> tuple[str, int] | None:
    offset = _skip_space(value, offset)
    if offset >= len(value) or value[offset] != "{":
        return None
    depth = 1
    start = offset + 1
    offset = start
    while offset < len(value):
        character = value[offset]
        if character == "%" and (offset == 0 or value[offset - 1] != "\\"):
            newline = value.find("\n", offset)
            if newline < 0:
                return None
            offset = newline + 1
            continue
        if character == "\\":
            offset += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return value[start:offset], offset + 1
        offset += 1
    return None


def _decode_mirror(value: str) -> str:
    named = {
        r"\textbackslash{}": "\\",
        r"\textasciicircum{}": "^",
        r"\textasciitilde{}": "~",
    }
    escaped = set("{}%#&$_")
    output: list[str] = []
    offset = 0
    while offset < len(value):
        matched = next(
            (
                (encoded, decoded)
                for encoded, decoded in named.items()
                if value.startswith(encoded, offset)
            ),
            None,
        )
        if matched is not None:
            encoded, decoded = matched
            output.append(decoded)
            offset += len(encoded)
        elif value[offset] == "\\" and offset + 1 < len(value) and value[offset + 1] in escaped:
            output.append(value[offset + 1])
            offset += 2
        else:
            output.append(value[offset])
            offset += 1
    return " ".join("".join(output).split())


def visible_comment_occurrences(value: str):
    """Retain duplicate live IDs, excluding literal examples and old proposal branches."""
    from thoughts_server.renderer_tools.review_source_staging import SourceScanner
    visibility = SourceScanner(value)
    visibility.scan()
    for match in COMMENT_ANCHOR_RE.finditer(value):
        start = match.start()
        if any(a < start < b for a, b in visibility.hidden):
            continue
        if _has_unescaped_percent(value[value.rfind("\n", 0, start) + 1:start]):
            continue
        identifier = _braced_argument(value, match.end())
        if identifier is None or not identifier[0].strip():
            continue
        if match.group() in {r"\CommentHighlight", r"\CommentStrikethrough"}:
            mirror = _braced_argument(value, identifier[1])
            if mirror is None or _braced_argument(value, mirror[1]) is None:
                continue
        yield identifier[0].strip(), value.count("\n", 0, start) + 1


def parse_review_comments(source_file: str, raw: bytes, *, include_kind: bool = False) -> list[dict[str, Any]]:
    """Parse visible CommentAnchor markers without evaluating TeX.

    A marker's mirror argument is ordinary-comment text.  Nested markers are
    deliberately scanned too, because overlapping highlighted comments are valid.
    ``include_kind`` adds the marker macro (or ``markdown``) for callers that
    record it; published manifests keep their existing record shape.
    """

    if source_file.endswith((".md", ".markdown")):
        from .markdown_source import MarkdownError, digest, parse_markdown
        try:
            parsed = parse_markdown(raw)
        except MarkdownError:
            # Invalid draft syntax is not evidence for deleting logical comments.
            return []
        comments = {}
        for key, marker in parsed.markers.items():
            if not key.startswith("comment:"):
                continue
            identifier = key.split(":")[1]
            comments.setdefault(identifier, {
                "id": identifier,
                "contents": parsed.metadata.get(key, {}).get("contents", ""),
                "source_file": source_file,
                "source_sha256": digest(raw),
                "line": raw[:marker["byte_start"]].count(b"\n") + 1,
                **({"kind": "markdown"} if include_kind else {}),
            })
        return list(comments.values())
    from thoughts_server.renderer_tools.review_source_staging import SourceScanner
    value = raw.decode("utf-8")
    visibility = SourceScanner(value)
    visibility.scan()
    comments: list[dict[str, Any]] = []
    cursor = 0
    while True:
        match = COMMENT_ANCHOR_RE.search(value, cursor)
        if match is None:
            break
        start = match.start()
        cursor = match.end()
        if any(a < start < b for a, b in visibility.hidden):
            continue
        if cursor < len(value) and (value[cursor].isalpha() or value[cursor] == "@"):
            continue
        line_start = value.rfind("\n", 0, start) + 1
        prefix = value[line_start:start]
        if _has_unescaped_percent(prefix):
            continue
        identifier = _braced_argument(value, cursor)
        if identifier is None:
            continue
        comment_id, after_id = identifier
        comment_id = comment_id.strip()
        if not comment_id or len(comment_id) > MAX_COMMENT_ID_LENGTH:
            continue
        mirror = _braced_argument(value, after_id)
        if mirror is None:
            continue
        contents, _after_mirror = mirror
        if match.group() in {r"\CommentHighlight", r"\CommentStrikethrough"} and _braced_argument(value, _after_mirror) is None:
            continue
        comments.append(
            {
                "id": comment_id,
                "contents": _decode_mirror(contents),
                "source_file": source_file,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "line": value.count("\n", 0, start) + 1,
                **({"kind": match.group()[1:]} if include_kind else {}),
            }
        )
    duplicates = Counter(comment["id"] for comment in comments)
    return [comment for comment in comments if duplicates[comment["id"]] == 1]


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _source_artifact_root(settings) -> Path:
    return settings.state_root / "source-bundle"


def _source_entry(source: SourceFile, raw: bytes, previous: dict[str, Any]) -> dict[str, Any]:
    entry = dict(previous)
    entry.update(
        {
            "path": source.path,
            "sha256": source.sha256,
            "object": source.object_sha256,
            "bytes": len(raw),
        }
    )
    return entry


def _refresh_pdf_input_hashes(pdf: Any, source_hashes: dict[str, str]) -> Any:
    """Update source identity metadata without changing the rendered PDF identity."""

    if not isinstance(pdf, dict):
        return pdf
    replacement = dict(pdf)
    segments = pdf.get("segments")
    if not isinstance(segments, list):
        return replacement
    refreshed_segments = []
    for value in segments:
        if not isinstance(value, dict):
            refreshed_segments.append(value)
            continue
        segment = dict(value)
        inputs = value.get("inputs")
        if isinstance(inputs, list):
            refreshed_inputs = []
            for input_value in inputs:
                if not isinstance(input_value, dict):
                    refreshed_inputs.append(input_value)
                    continue
                input_entry = dict(input_value)
                source_path = input_entry.get("source_file")
                if source_path in source_hashes:
                    input_entry["source_sha256"] = source_hashes[source_path]
                refreshed_inputs.append(input_entry)
            segment["inputs"] = refreshed_inputs
        refreshed_segments.append(segment)
    replacement["segments"] = refreshed_segments
    return replacement



def refresh_document_review_comments(
    settings,
    session: Session,
    store: ObjectStore,
    changed_paths: set[str],
    *,
    proposal_resolution: bool = False,
    rebuild_requested_by: str | None = None,
) -> dict[str, str]:
    """Publish current CommentAnchor projections for affected publications.

    With [rebuild_requested_by], a PDF kept across an accepted proposal is
    also queued for a debounced rebuild, since its SyncTeX names the removed
    wrapper lines rather than the accepted text.

    The PDF and all PDF-derived artifact fields are retained byte-for-byte.
    Only source objects, source-linked proposal hashes, and review comments are
    replaced, so this operation cannot trigger or stand in for a render.
    """

    if not changed_paths:
        return {}
    active_sources = {
        source.path: source
        for source in session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False)))
    }
    logical_entities = {
        entity.entity_id: entity.payload
        for entity in session.scalars(
            select(Entity).where(
                Entity.entity_type == "logical_comment",
                Entity.collection == "comments",
            )
        )
    }
    raw_sources: dict[str, bytes] = {}
    root = _source_artifact_root(settings)
    refreshed: dict[str, str] = {}
    publications = session.scalars(
        select(Publication).where(Publication.source_artifact_sha256.is_not(None))
    ).all()
    for publication in publications:
        artifact_sha256 = publication.source_artifact_sha256
        if not artifact_sha256:
            continue
        try:
            manifest = json.loads(store.path(artifact_sha256).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("kind") != "thoughts-document-source-artifact"
            or manifest.get("filename") != publication.filename
            or not isinstance(manifest.get("sources"), list)
        ):
            continue
        source_paths = {
            value.get("path")
            for value in manifest["sources"]
            if isinstance(value, dict) and isinstance(value.get("path"), str)
        }
        if not source_paths.intersection(changed_paths):
            continue
        if not source_paths.issubset(active_sources):
            continue
        for path in source_paths:
            if path not in raw_sources:
                raw_sources[path] = store.path(active_sources[path].object_sha256).read_bytes()
        # Markdown evidence binds exact source bytes. Retain the last-good pair
        # until rendering completes, including metadata-only source edits.
        if manifest.get("review_placement_model_version") == 4:
            continue
        compiler_refresh = None
        if manifest.get("review_placement_model_version") == 3:
            compiler_refresh = metadata_refresh(
                manifest,
                {path: raw_sources[path] for path in source_paths},
                lambda identity: store.path(identity).read_bytes(),
            )
            if compiler_refresh is None:
                # Preserve the complete last-good PDF/artifact pair while its new
                # target geometry builds; logical operations are a separate stream.
                continue
            if (
                proposal_resolution
                and rebuild_requested_by
                and any(
                    item.get("method") == "proposal_accept_metadata_v1"
                    for item in compiler_refresh.get("source_revision_equivalences", [])
                )
            ):
                from .proposal_renders import defer_accepted_pdf_rebuild

                defer_accepted_pdf_rebuild(session, rebuild_requested_by, publication.filename)
        sources = [
            _source_entry(active_sources[path], raw_sources[path], value)
            for value in manifest["sources"]
            if isinstance(value, dict) and (path := value.get("path")) in active_sources
        ]
        review_comments = [
            comment
            for path in sorted(source_paths)
            for comment in parse_review_comments(path, raw_sources[path])
        ]
        proposal_source_hashes = {path: active_sources[path].sha256 for path in source_paths}
        proposals = []
        for value in manifest.get("agent_proposals", []):
            if not isinstance(value, dict):
                continue
            proposal = dict(value)
            source_file = proposal.get("source_file")
            if source_file in proposal_source_hashes:
                proposal["source_sha256"] = proposal_source_hashes[source_file]
            proposals.append(proposal)
        replacement = compiler_refresh if compiler_refresh is not None else dict(manifest)
        replacement["sources"] = sources
        if compiler_refresh is None:
            replacement["pdf"] = _refresh_pdf_input_hashes(manifest.get("pdf"), proposal_source_hashes)
            replacement["agent_proposals"] = proposals
        replacement["review_comments_schema_version"] = 1
        replacement["review_comments"] = review_comments
        roots = {comment["id"]: comment for comment in review_comments}
        previous_logical = {
            str(value.get("comment_id") or ""): value
            for value in manifest.get("logical_comments", [])
            if isinstance(value, dict)
        }
        logical_comments = []
        for comment_id, payload in sorted(logical_entities.items()):
            root_id = str(payload.get("thread_root_id") or comment_id)
            thread_root = logical_entities.get(root_id, payload)
            anchor_id = str((thread_root.get("scope") or {}).get("anchor_id") or root_id)
            origin = payload.get("origin") or {}
            if anchor_id not in roots and comment_id not in previous_logical and f"{origin.get('collection')}.pdf" != publication.filename:
                continue
            logical_comments.append(comment_record(payload, thread_root))
        replacement["logical_comments_schema_version"] = 1
        replacement["logical_comments"] = logical_comments
        if manifest.get("review_correspondence_schema_version") == 2 and compiler_refresh is None:
            refreshed_source_set = source_set_sha256(sources)
            correspondences = []
            for value in manifest.get("review_correspondences", []):
                if not isinstance(value, dict):
                    continue
                correspondence = dict(value)
                if correspondence.get("state") != "no_source":
                    correspondence.update(
                        {
                            "state": "needs_attention",
                            "reason": "source changed after the last immutable PDF build",
                            "locations": [],
                            "source_bundle_sha256": refreshed_source_set,
                        }
                    )
                correspondences.append(correspondence)
            failures = strict_gate_failures(correspondences)
            replacement["review_correspondence_schema_version"] = 2
            replacement["review_correspondences"] = correspondences
            replacement["review_correspondence_gate"] = {
                "strict": False,
                "eligible": not failures,
                "failure_count": len(failures),
                "failures": failures,
                "reason": "source_refresh_requires_render",
            }
        if (
            proposal_resolution
            and compiler_refresh is not None
            and replacement["sources"] != manifest["sources"]
        ):
            # Queued decisions on other, unchanged sources may still refer to
            # this artifact. Source hashes are checked again before applying.
            replacement["proposal_resolution_predecessors"] = sorted(
                {
                    *manifest.get("proposal_resolution_predecessors", []),
                    artifact_sha256,
                }
            )
        # Proposal identities include current source offsets and hashes. Keep the
        # object references consistent with the refreshed inline proposals.
        proposal_objects = []
        for proposal in replacement.get("agent_proposals", []):
            proposal_raw = (
                json.dumps(
                    {"schema_version": 1, "kind": "thoughts-agent-proposal", **proposal},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            record = store.put_bytes(session, proposal_raw, "application/json")
            _atomic_write(root / "objects" / record.sha256, proposal_raw)
            proposal_objects.append({"id": proposal["id"], "object": record.sha256})
        replacement["proposal_objects"] = proposal_objects
        raw = (
            json.dumps(replacement, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        new_sha256 = hashlib.sha256(raw).hexdigest()
        if new_sha256 == artifact_sha256:
            continue
        store.put_bytes(session, raw, "application/vnd.thoughts.document-source+json")
        _atomic_write(root / "objects" / new_sha256, raw)
        for source in sources:
            object_sha256 = source["object"]
            _atomic_write(root / "objects" / object_sha256, raw_sources[source["path"]])
        pointer = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact-pointer",
            "filename": publication.filename,
            "artifact_sha256": new_sha256,
            "manifest_object": new_sha256,
            "pdf_sha256": publication.object_sha256,
        }
        _atomic_write(
            root / "documents" / f"{publication.filename}.json",
            (json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        publication.source_artifact_sha256 = new_sha256
        metadata = dict(publication.metadata_json or {})
        metadata["source_artifact_sha256"] = new_sha256
        publication.metadata_json = metadata
        publication.published_at = utc_now()
        refreshed[publication.filename] = new_sha256
        if compiler_refresh is not None and replacement.get("review_card_anchors_schema_version") == 1:
            from .review_reanchor import project_review_anchors_for_publication

            project_review_anchors_for_publication(
                session, publication.filename, publication.object_sha256,
                replacement, store=store, metadata_only=True,
                previous_source_manifest=manifest,
            )
    if refreshed:
        # Publication metadata carries the source-artifact identity, so keep the
        # compact library source-layout index in lockstep with this source-only
        # update.  This is an object-store refresh, not a render request.
        store_published_source_index(session, store)
    return refreshed
