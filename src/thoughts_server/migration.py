# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session

from .auth import token_hash
from .library import (load_library_catalogue, store_published_source_index, workspace_source_path,
                      write_library_index)
from .models import Device, Entity, Operation, Publication, new_document_id
from .object_store import ObjectStore
from .operations import apply_operation
from .schemas import OperationInput

IMPORT_NAMESPACE = uuid.UUID("6aac70dd-2f4c-4adb-b706-f1a45120a84f")
EXPORT_ENTITY_TYPES = {
    "annotation",
    "comment",
    "logical_comment",
    "proposal_event",
    "review_correspondence_review",
    "resolution",
    "review_card",
    "review_event",
    "review_exclusion",
}


@dataclass
class MigrationReport:
    comments: int = 0
    resolutions: int = 0
    review_cards: int = 0
    review_events: int = 0
    review_exclusions: int = 0
    review_correspondence_reviews: int = 0
    pdfs: int = 0
    unverified_pdfs: int = 0
    auxiliary_objects: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "comments": self.comments,
            "resolutions": self.resolutions,
            "review_cards": self.review_cards,
            "review_events": self.review_events,
            "review_exclusions": self.review_exclusions,
            "review_correspondence_reviews": self.review_correspondence_reviews,
            "pdfs": self.pdfs,
            "unverified_pdfs": self.unverified_pdfs,
            "auxiliary_objects": self.auxiliary_objects,
            "duplicates": self.duplicates,
            "errors": self.errors,
        }


@dataclass(frozen=True)
class ExportEntity:
    entity_type: str
    collection: str
    entity_id: str
    payload: dict


def _json_lines(path: Path, report: MigrationReport):
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    report.errors.append(f"{path}:{line_number}: {error}")
                    continue
                if not isinstance(value, dict):
                    report.errors.append(f"{path}:{line_number}: record is not an object")
                    continue
                yield line_number, value
    except OSError as error:
        report.errors.append(f"{path}: {error}")


def _json_files(root: Path, report: MigrationReport):
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            report.errors.append(f"{path}: {error}")
            continue
        if not isinstance(value, dict):
            report.errors.append(f"{path}: record is not an object")
            continue
        yield path, value


def _identity(payload: dict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _safe_export_component(value: str) -> str:
    if value not in {"", ".", ".."} and "/" not in value and "\\" not in value and "\0" not in value:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"escaped-{digest}"


class WorkspaceImporter:
    def __init__(self, docs_root: Path, session: Session, store: ObjectStore):
        self.docs_root = docs_root.resolve()
        self.session = session
        self.store = store
        self.report = MigrationReport()
        self.sequence = int(
            session.scalar(
                select(func.max(Operation.device_sequence)).where(Operation.device_id == "legacy-import")
            )
            or 0
        )

    def _ensure_device(self) -> None:
        if self.session.get(Device, "legacy-import") is None:
            self.session.add(
                Device(
                    id="legacy-import",
                    name="Legacy workspace import",
                    token_hash=token_hash(f"legacy-import-disabled-{uuid.uuid4()}"),
                    enabled=False,
                )
            )
            self.session.flush()

    def _operation(
        self,
        entity_type: str,
        entity_id: str,
        collection: str,
        payload: dict,
        provenance: str,
    ) -> None:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        operation_id = str(
            uuid.uuid5(IMPORT_NAMESPACE, f"{entity_type}\n{collection}\n{entity_id}\n{canonical}")
        )
        existing = self.session.scalar(select(Operation).where(Operation.operation_id == operation_id))
        if existing is None:
            self.sequence += 1
            device_sequence = self.sequence
        else:
            device_sequence = existing.device_sequence
        timestamp = str(
            payload.get("updated_at")
            or payload.get("reviewed_at")
            or payload.get("modified")
            or payload.get("created")
            or "1970-01-01T00:00:00Z"
        )
        result = apply_operation(
            self.session,
            "legacy-import",
            OperationInput(
                operation_id=operation_id,
                device_sequence=device_sequence,
                entity_type=entity_type,
                entity_id=entity_id,
                collection=collection,
                base_revision=None,
                client_timestamp=timestamp,
                payload=payload,
            ),
            provenance=provenance,
        )
        if result.outcome == "duplicate":
            self.report.duplicates += 1

    def import_metadata(self) -> None:
        self._ensure_device()
        comments_root = self.docs_root / "comments"
        for path in sorted(comments_root.glob("*_comments.jsonl")):
            if path.name == "logical_comments.jsonl":
                continue
            collection = path.name.removesuffix("_comments.jsonl")
            for line_number, payload in _json_lines(path, self.report):
                entity_id = _identity(payload, "id", "annotation_id", "comment_key")
                if entity_id is None:
                    self.report.errors.append(f"{path}:{line_number}: comment has no stable identity")
                    continue
                self._operation("comment", entity_id, collection, payload, f"{path}:{line_number}")
                self.report.comments += 1
        correspondence_path = comments_root / "review_correspondence_reviews.jsonl"
        if correspondence_path.is_file():
            for line_number, payload in _json_lines(correspondence_path, self.report):
                entity_id = _identity(payload, "review_key")
                if entity_id is None:
                    self.report.errors.append(
                        f"{correspondence_path}:{line_number}: correspondence review has no identity"
                    )
                    continue
                self._operation(
                    "review_correspondence_review",
                    entity_id,
                    "review-correspondence",
                    payload,
                    f"{correspondence_path}:{line_number}",
                )
                self.report.review_correspondence_reviews += 1
        resolution_path = comments_root / "review_resolutions.jsonl"
        if resolution_path.is_file():
            for line_number, payload in _json_lines(resolution_path, self.report):
                entity_id = _identity(payload, "id", "annotation_id", "comment_key")
                if entity_id is None:
                    self.report.errors.append(f"{resolution_path}:{line_number}: resolution has no identity")
                    continue
                self._operation(
                    "resolution", entity_id, "global", payload, f"{resolution_path}:{line_number}"
                )
                self.report.resolutions += 1
        review_mapping = {
            "cards": ("review_card", "card_id", "review_cards"),
            "events": ("review_event", "event_id", "review_events"),
            "exclusions": ("review_exclusion", "exclusion_id", "review_exclusions"),
        }
        for directory, (entity_type, identity_key, counter) in review_mapping.items():
            for path, payload in _json_files(self.docs_root / "reviews" / directory, self.report) or ():
                entity_id = _identity(payload, identity_key)
                if entity_id is None:
                    entity_id = str(
                        uuid.uuid5(IMPORT_NAMESPACE, f"{path}\n{json.dumps(payload, sort_keys=True)}")
                    )
                self._operation(entity_type, entity_id, directory, payload, str(path))
                setattr(self.report, counter, getattr(self.report, counter) + 1)

        phone_events = self.docs_root.parent / "phone-to-mac" / "pdf-review-events"
        for path, payload in _json_files(phone_events, self.report) or ():
            entity_id = _identity(payload, "event_id")
            if entity_id is None:
                self.report.errors.append(f"{path}: review event has no event_id")
                continue
            self._operation("review_event", entity_id, "events", payload, str(path))
            self.report.review_events += 1

    def artifact_entries(self) -> list[tuple[Path, dict]]:
        catalogue = load_library_catalogue(self.docs_root)
        self.report.errors.extend(catalogue.errors)
        self.report.unverified_pdfs += catalogue.unverified_pdfs
        entries = [(entry.path, entry.metadata) for entry in catalogue.entries]
        self.report.pdfs = len(catalogue.entries)
        self.report.auxiliary_objects = sum(
            int((self.docs_root / relative).is_file())
            for relative in (
                "current_pdfs/.thoughts-source-index.json",
                "current_pdfs/.thoughts-library-index.json",
                "comments/review_resolutions.jsonl",
            )
        )
        return entries

    def import_artifacts(self, entries: list[tuple[Path, dict]]) -> None:
        for path, metadata in entries:
            record = self.store.ingest_file(self.session, path, "application/pdf")
            publication = self.session.get(Publication, path.name)
            if publication is None:
                from .document_identity import register_document
                identifier = new_document_id()
                register_document(self.session, identifier, path.name, workspace_source_path(
                    self.docs_root, str(metadata.get("project", "")), path.name))
                publication = Publication(
                    filename=path.name,
                    document_id=identifier,
                    object_sha256=record.sha256,
                    size_bytes=record.size_bytes,
                )
                self.session.add(publication)
            publication.object_sha256 = record.sha256
            publication.size_bytes = record.size_bytes
            publication.project = str(metadata.get("project", ""))
            publication.metadata_json = metadata
        for relative in (
            "current_pdfs/.thoughts-source-index.json",
            "current_pdfs/.thoughts-library-index.json",
            "comments/review_resolutions.jsonl",
        ):
            path = self.docs_root / relative
            if path.is_file():
                self.store.ingest_file(self.session, path, "application/json")

    def run(self, commit: bool) -> MigrationReport:
        self.import_metadata()
        entries = self.artifact_entries()
        if commit and not self.report.errors:
            self.import_artifacts(entries)
            store_published_source_index(self.session, self.store)
            self.session.commit()
            write_library_index(self.session, self.docs_root)
        else:
            self.session.rollback()
        return self.report


def _atomic_write(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            Path(temporary).unlink()
    return True


# What an export with selected collections writes besides those collections' own files.
_ALWAYS_EXPORTED_TYPES = frozenset({"resolution", "logical_comment", "review_correspondence_review", "proposal_event"})
_REVIEW_DATA_TYPES = frozenset({"review_card", "review_event", "review_exclusion"})


def _export_scope(
    export_types: set[str],
    selected_collections: set[str] | None,
    include_review_data: bool,
) -> tuple[set[str], set[str] | None]:
    """The entity types, and the annotation/comment collections, an export can write.

    Exactly the rows ``export_workspace`` keeps after reading: with selected
    collections it keeps the always-exported types and the annotations and comments
    of those collections; without review data it writes no card, event or exclusion.
    """
    types = set(export_types)
    if selected_collections is not None:
        types &= _ALWAYS_EXPORTED_TYPES | {"annotation", "comment"}
    elif not include_review_data:
        types -= _REVIEW_DATA_TYPES
    return types, (set(selected_collections) if selected_collections is not None else None)


def export_workspace(
    session: Session,
    destination: Path,
    *,
    write_manifest: bool = True,
    frontier: int | None = None,
    prune: bool = False,
    selected_collections: set[str] | None = None,
    include_review_data: bool = True,
    include_proposal_history: bool = True,
) -> dict[str, int]:
    destination = destination.resolve()
    counts: dict[str, int] = {}
    export_types = EXPORT_ENTITY_TYPES if include_proposal_history else EXPORT_ENTITY_TYPES - {"proposal_event"}
    # Read only the rows this export writes. The filters below still decide what is
    # written; the scope only keeps the database from returning rows they would drop
    # (a targeted build exports one document's comments, not every review event).
    scoped_types, scoped_collections = _export_scope(export_types, selected_collections, include_review_data)

    def in_scope(model):
        conditions = [model.entity_type.in_(scoped_types)]
        if scoped_collections is not None:
            conditions.append(or_(
                model.entity_type.notin_(("annotation", "comment")),
                model.collection.in_(sorted(scoped_collections)),
            ))
        return conditions

    if frontier is None:
        entities = [
            ExportEntity(row.entity_type, row.collection, row.entity_id, row.payload)
            for row in session.scalars(
                select(Entity)
                .where(*in_scope(Entity))
                .order_by(Entity.entity_type, Entity.collection, Entity.entity_id)
            )
        ]
    else:
        # Fetch only the winning payload per entity at the frozen frontier.
        # Older operations are immutable history, not renderer inputs.
        ranked = select(
            Operation.operation_id,
            func.row_number().over(
                partition_by=(Operation.entity_type, Operation.collection, Operation.entity_id),
                order_by=Operation.server_sequence.desc(),
            ).label("position"),
        ).where(
            Operation.server_sequence <= frontier,
            *in_scope(Operation),
            Operation.outcome == "applied",
        ).subquery()
        entities = [ExportEntity(row.entity_type, row.collection, row.entity_id, row.payload)
                    for row in session.scalars(select(Operation).join(
                        ranked, Operation.operation_id == ranked.c.operation_id,
                    ).where(ranked.c.position == 1).order_by(
                        Operation.entity_type, Operation.collection, Operation.entity_id))]
    if selected_collections is not None:
        entities = [
            entity
            for entity in entities
            if entity.entity_type == "resolution"
            or entity.entity_type == "logical_comment"
            or entity.entity_type == "review_correspondence_review"
            or entity.entity_type == "proposal_event"
            or (entity.entity_type in {"annotation", "comment"} and entity.collection in selected_collections)
        ]
    # Historical placements can outlive their canonical comment tombstone.
    # Never republish those placements when reconstructing a build frontier.
    deleted_comments = {
        entity.entity_id for entity in entities
        if entity.entity_type == "logical_comment" and entity.payload.get("deleted") is True
    }
    annotations = [
        ExportEntity(entity.entity_type, entity.collection, entity.entity_id,
                     {**entity.payload, "deleted": True} if entity.entity_id in deleted_comments else entity.payload)
        for entity in entities if entity.entity_type in {"annotation", "comment"}
    ]
    comments = [
        entity
        for entity in annotations
        if entity.entity_type == "comment"
        or entity.payload.get("discussion") is True
        or "contents" in entity.payload
    ]
    collections: dict[str, list[dict]] = {}
    for entity in comments:
        collections.setdefault(entity.collection, []).append(entity.payload)
    if prune:
        expected = {f"{_safe_export_component(collection)}_comments.jsonl" for collection in collections}
        for path in (destination / "comments").glob("*_comments.jsonl"):
            if path.name == "logical_comments.jsonl":
                continue
            collection = path.name.removesuffix("_comments.jsonl")
            if path.name not in expected and (
                selected_collections is None or collection in selected_collections
            ):
                path.unlink()
    for collection, records in collections.items():
        raw = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
        filename = f"{_safe_export_component(collection)}_comments.jsonl"
        _atomic_write(destination / "comments" / filename, raw.encode("utf-8"))
    counts["comments"] = len(comments)
    logical_comments = [entity.payload for entity in entities if entity.entity_type == "logical_comment"]
    logical_raw = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in logical_comments
    )
    _atomic_write(destination / "comments" / "logical_comments.jsonl", logical_raw.encode("utf-8"))
    counts["logical_comments"] = len(logical_comments)
    if include_proposal_history:
        proposal_events = [entity.payload for entity in entities if entity.entity_type == "proposal_event"]
        proposal_raw = "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in proposal_events)
        _atomic_write(destination / "comments" / "proposal_events.jsonl", proposal_raw.encode("utf-8"))
        counts["proposal_events"] = len(proposal_events)
    correspondence_reviews = [
        entity.payload for entity in entities if entity.entity_type == "review_correspondence_review"
    ]
    correspondence_review_raw = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in correspondence_reviews
    )
    _atomic_write(
        destination / "comments" / "review_correspondence_reviews.jsonl",
        correspondence_review_raw.encode("utf-8"),
    )
    counts["review_correspondence_reviews"] = len(correspondence_reviews)
    annotation_collections: dict[str, list[dict]] = {}
    for entity in annotations:
        annotation_collections.setdefault(entity.collection, []).append(entity.payload)
    if prune:
        expected = {
            f"{_safe_export_component(collection)}_annotations.jsonl" for collection in annotation_collections
        }
        for path in (destination / "annotations").glob("*_annotations.jsonl"):
            collection = path.name.removesuffix("_annotations.jsonl")
            if path.name not in expected and (
                selected_collections is None or collection in selected_collections
            ):
                path.unlink()
    for collection, records in annotation_collections.items():
        raw = "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records)
        filename = f"{_safe_export_component(collection)}_annotations.jsonl"
        _atomic_write(destination / "annotations" / filename, raw.encode("utf-8"))
    counts["annotations"] = len(annotations)
    resolutions = [entity for entity in entities if entity.entity_type == "resolution"]
    raw_resolutions = "".join(
        json.dumps(entity.payload, ensure_ascii=False, sort_keys=True) + "\n" for entity in resolutions
    )
    _atomic_write(destination / "comments" / "review_resolutions.jsonl", raw_resolutions.encode("utf-8"))
    counts["resolutions"] = len(resolutions)
    for entity_type, directory in (
        ("review_card", "cards"),
        ("review_event", "events"),
        ("review_exclusion", "exclusions"),
    ):
        if not include_review_data:
            continue
        typed_entities = [entity for entity in entities if entity.entity_type == entity_type]
        if prune:
            expected = {f"{_safe_export_component(entity.entity_id)}.json" for entity in typed_entities}
            for path in (destination / "reviews" / directory).glob("*.json"):
                if path.name not in expected:
                    path.unlink()
        for entity in typed_entities:
            _atomic_write(
                destination / "reviews" / directory / f"{_safe_export_component(entity.entity_id)}.json",
                (json.dumps(entity.payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )
        counts[entity_type] = len(typed_entities)
    if write_manifest:
        _atomic_write(
            destination / "export-manifest.json",
            (json.dumps({"schema_version": 1, "counts": counts}, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
    return counts
