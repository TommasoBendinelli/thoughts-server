# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import uuid
import hashlib
import json
from contextlib import ExitStack

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    AgentProposalResolution,
    AnnotationAnchorJob,
    Entity,
    Operation,
    Publication,
    utc_now,
)
from .object_store import ObjectStore
from .schemas import (
    OperationInput,
    OperationResult,
)


class OperationConflict(ValueError):
    pass


FEEDBACK_IMMUTABLE_FIELDS = ("card_id", "anchor", "created_at", "created_origin_id", "view", "html_edition_sha256")
# protocol/document-feedback.md section 3: the same rules without a card or an HTML edition.
DOCUMENT_FEEDBACK_IMMUTABLE_FIELDS = ("anchor", "created_at", "created_origin_id", "view")
IMMUTABLE_FIELDS_BY_FEEDBACK_TYPE = {
    "review_card_feedback": FEEDBACK_IMMUTABLE_FIELDS,
    "document_feedback": DOCUMENT_FEEDBACK_IMMUTABLE_FIELDS,
}


def _integer_revision(payload: dict, fallback: int) -> int:
    value = payload.get("revision", fallback)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise OperationConflict("payload revision must be a positive integer")
    return value


def _priority(operation: OperationInput, revision: int) -> tuple[int, str, str, str]:
    origin = operation.payload.get("origin_id", "")
    return (
        revision,
        operation.payload.get("updated_at", operation.client_timestamp),
        str(origin),
        operation.operation_id,
    )


def _stored_priority(entity: Entity) -> tuple[int, str, str, str]:
    return (
        entity.revision,
        str(entity.payload.get("updated_at", "")),
        str(entity.payload.get("origin_id", "")),
        entity.winner_operation_id,
    )


def _same_operation_payload(existing: Operation, incoming: OperationInput, session: Session | None = None) -> bool:
    """Compare submitted content while ignoring server-owned logical revisions."""

    if existing.entity_type != "logical_comment":
        return existing.payload == incoming.payload
    submitted = existing.payload.get("_source_submission_sha256")
    if submitted is not None:
        from .default_folder_segment import alias_active, legacy_value
        from .source_folder_names import legacy_spellings
        # A retry rewritten by the default-folder or source-folder alias hashes like its original.
        candidates = legacy_spellings(session, incoming.payload)
        if alias_active():
            candidates += [legacy_value(value) for value in list(candidates)]
        return submitted in {hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                             for value in candidates}
    stored_payload = dict(existing.payload)
    incoming_payload = dict(incoming.payload)
    for field in ("revision", "content_updated_at"):
        stored_payload.pop(field, None)
        incoming_payload.pop(field, None)
    if existing.provenance is None and (existing.base_revision or 0) > 0:
        for field in ("scope", "created_at", "linking"):
            stored_payload.pop(field, None)
            incoming_payload.pop(field, None)
    return stored_payload == incoming_payload


def latest_sequence(session: Session) -> int:
    return int(session.scalar(select(func.max(Operation.server_sequence))) or 0)


def latest_device_sequence(session: Session, device_id: str) -> int:
    operation_sequence = session.scalar(
        select(func.max(Operation.device_sequence)).where(Operation.device_id == device_id)
    )
    proposal_sequence = session.scalar(
        select(func.max(AgentProposalResolution.device_sequence)).where(
            AgentProposalResolution.device_id == device_id
        )
    )
    return max(int(operation_sequence or 0), int(proposal_sequence or 0))


def projection_entity_type(entity_type: str) -> str:
    """Legacy comment operations feed the single annotation projection."""

    return "annotation" if entity_type == "comment" else entity_type


def _validate_logical_comment_operation(
    session: Session,
    incoming: OperationInput,
    provenance: str | None,
) -> None:
    if incoming.entity_type != "logical_comment":
        return
    if incoming.collection != "comments":
        raise OperationConflict("logical comments must use the comments collection")
    payload = incoming.payload
    existing = session.get(Entity, ("logical_comment", "comments", incoming.entity_id))
    parent_id = payload.get("parent_id")
    if isinstance(parent_id, str) and parent_id:
        parent = session.get(Entity, ("logical_comment", "comments", parent_id))
        system_tombstone = provenance is not None and payload.get("deleted") is True
        already_deleted_reply = (
            parent is not None and parent.deleted and existing is not None
            and existing.deleted and payload.get("deleted") is True
        )
        if (parent is None or parent.deleted) and not (system_tombstone or already_deleted_reply):
            raise OperationConflict("logical comment reply references a missing parent")
        if parent is not None and payload.get("thread_root_id") != parent.payload.get(
            "thread_root_id", parent.entity_id
        ):
            raise OperationConflict("logical comment reply has an inconsistent thread root")
    if existing is None:
        if provenance is None and payload.get("source_authority") != "client-v1" and (payload.get("scope") or {}).get("state") not in {"pdf_only", "standalone"}:
            raise OperationConflict("clients may only create PDF-only comments or standalone notes")
        return
    if (payload.get("subtype") == "Note") != (existing.payload.get("subtype") == "Note"):
        raise OperationConflict("a note cannot be converted into a document comment")
    identity_migration = bool(
        provenance and provenance.startswith("logical-comment-migration-detach:")
    )
    for immutable in ("comment_id", "thread_root_id", "parent_id", "origin"):
        document_move = immutable == "origin" and bool(provenance and provenance.startswith("document-move:"))
        if not identity_migration and not document_move and payload.get(immutable) != existing.payload.get(immutable):
            raise OperationConflict(f"logical comment {immutable} is immutable")
    # Scope is a server-owned placement mirror. A client may have an older
    # mirror while its content revision is current; ignore it on mutation.


_CARD_LINK_FIELDS = {"source_link", "reanchor", "source_change", "revision", "updated_at", "origin_id"}


def _without_card_link(payload: dict) -> dict:
    stripped = {key: value for key, value in payload.items() if key not in _CARD_LINK_FIELDS}
    primary = dict(stripped.get("primary") or {})
    primary.pop("source_anchor", None)
    stripped["primary"] = primary
    return stripped


def _rebased_manual_source_link(
    session: Session, incoming: OperationInput, entity: Entity | None,
) -> dict | None:
    """Apply a device's manual source link over newer server-only revisions of the card.

    Placement jobs advance a card's revision while a device is offline. A manual link
    based on an older revision would otherwise lose to them although nobody edited
    the card. Any other change, or any intervening device edit, keeps the ordinary rules.
    """
    base = incoming.base_revision
    payload = incoming.payload
    if entity is None or entity.deleted or base is None or not 0 < base < entity.revision:
        return None
    link = payload.get("source_link")
    anchor = (payload.get("primary") or {}).get("source_anchor")
    current = entity.payload
    if not (isinstance(link, dict) and link.get("state") == "linked" and isinstance(anchor, dict)):
        return None
    if ((current.get("primary") or {}).get("source_anchor")
            or (current.get("source_link") or {}).get("state") != "linking_failed"):
        return None
    later = list(session.scalars(select(Operation).where(
        Operation.entity_type == "review_card", Operation.collection == incoming.collection,
        Operation.entity_id == incoming.entity_id, Operation.outcome == "applied",
        Operation.entity_revision > base)))
    if not later or any(operation.provenance is None for operation in later):
        return None
    based_on = session.scalar(select(Operation).where(
        Operation.entity_type == "review_card", Operation.collection == incoming.collection,
        Operation.entity_id == incoming.entity_id, Operation.outcome == "applied",
        Operation.entity_revision == base))
    if based_on is None or _without_card_link(based_on.payload) != _without_card_link(payload):
        return None
    rebased = copy.deepcopy(current)
    rebased.pop("reanchor", None)
    rebased.pop("source_change", None)
    rebased["primary"] = {**(rebased.get("primary") or {}), "source_anchor": copy.deepcopy(anchor)}
    rebased["source_link"] = copy.deepcopy(link)
    rebased["revision"] = entity.revision + 1
    rebased["updated_at"] = payload.get("updated_at", incoming.client_timestamp)
    rebased["origin_id"] = payload.get("origin_id", "")
    return rebased


PROPOSAL_NOT_REMOVED = "The proposal is no longer removed outside Thoughts"


def _dismissed_proposal(session: Session, incoming: OperationInput) -> dict:
    """Validate a client Dismiss and return the retired row (protocol/agent-proposals.md 10.6).

    Dismiss is the only client operation on a current proposal: it retires a
    ``removed_outside`` row whose revision is the operation's ``base_revision``.
    """
    payload = incoming.payload
    if (payload.get("status") != "dismissed" or payload.get("deleted") is not True
            or payload.get("kind", "dismissed") != "dismissed"):
        raise OperationConflict("current proposals are reconciled from canonical source")
    entity = session.get(Entity, ("proposal", "proposals", incoming.entity_id))
    if entity is None or entity.deleted or entity.payload.get("status") != "removed_outside":
        raise OperationConflict(PROPOSAL_NOT_REMOVED)
    if incoming.base_revision != entity.revision:
        raise OperationConflict(
            f"{PROPOSAL_NOT_REMOVED} at revision {incoming.base_revision}; it is at revision {entity.revision}"
        )
    dismissed = {key: value for key, value in entity.payload.items() if key != "removal"}
    dismissed.update(
        status="dismissed", kind="dismissed", deleted=True, source_present=False,
        revision=entity.revision + 1,
        updated_at=str(payload.get("updated_at") or incoming.client_timestamp),
        last_event_id=f"dismiss:{incoming.operation_id}",
    )
    return dismissed


def apply_operation(
    session: Session,
    device_id: str,
    incoming: OperationInput,
    *,
    provenance: str | None = None,
    object_store: ObjectStore | None = None,
    allow_source_delete: bool = False,
) -> OperationResult:
    from .models import DocumentIdentityMigration

    if session.get(DocumentIdentityMigration, "permanent-document-identity-v1") is not None:
        identity = None
        if incoming.entity_type == "annotation":
            identity = incoming.payload
        elif incoming.entity_type == "review_card":
            identity = incoming.payload.get("primary") or {}
        elif incoming.entity_type == "logical_comment" and (incoming.payload.get("scope") or {}).get("state") != "standalone":
            identity = incoming.payload.get("origin") or {}
        elif incoming.entity_type == "pdf_link_override":
            identity = {"document_id": incoming.payload.get("source_document_id")}
        if identity is not None:
            identifier = identity.get("document_id")
            try:
                parsed = uuid.UUID(identifier)
                valid = parsed.version == 4 and str(parsed) == identifier
            except (ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise OperationConflict("This client must complete the permanent document identity migration before sending document changes")
    if incoming.entity_type == "proposal_event":
        # Proposal history is no longer kept. Older clients still upload their
        # observations; acknowledge them so their queues drain, and store nothing.
        return OperationResult(
            operation_id=incoming.operation_id,
            server_sequence=latest_sequence(session),
            outcome="superseded",
            entity_revision=1,
        )
    if "delete_source" in incoming.payload and not allow_source_delete:
        raise OperationConflict("Use the review source deletion endpoint for this operation")
    existing = session.scalar(select(Operation).where(Operation.operation_id == incoming.operation_id))
    if existing is not None:
        if (
            existing.device_id != device_id
            or existing.entity_type != incoming.entity_type
            or existing.entity_id != incoming.entity_id
            or existing.collection != incoming.collection
            or existing.base_revision != incoming.base_revision
            or existing.client_timestamp != incoming.client_timestamp
            or not _same_operation_payload(existing, incoming, session)
        ):
            raise OperationConflict("operation ID was reused with different content")
        if existing.device_sequence != incoming.device_sequence:
            occupied_sequence = session.scalar(
                select(Operation).where(
                    Operation.device_id == device_id,
                    Operation.device_sequence == incoming.device_sequence,
                )
            )
            if occupied_sequence is not None:
                raise OperationConflict(
                    f"device sequence {incoming.device_sequence} is already assigned to another operation"
                )
        entity = session.get(
            Entity,
            (
                projection_entity_type(existing.entity_type),
                existing.collection,
                existing.entity_id,
            ),
        )
        anchor_job = session.scalar(
            select(AnnotationAnchorJob).where(
                AnnotationAnchorJob.input_operation_id == incoming.operation_id
            )
        )
        return OperationResult(
            operation_id=incoming.operation_id,
            server_sequence=existing.server_sequence,
            outcome="duplicate" if existing.outcome == "applied" else existing.outcome,
            entity_revision=int(existing.entity_revision or (entity.revision if entity else 1)),
            anchor_job_id=anchor_job.id if anchor_job else None,
        )
    occupied_sequence = session.scalar(
        select(Operation).where(
            Operation.device_id == device_id,
            Operation.device_sequence == incoming.device_sequence,
        )
    )
    if occupied_sequence is not None:
        raise OperationConflict(
            f"device sequence {incoming.device_sequence} is already assigned to another operation"
        )
    if (incoming.entity_type == "review_card" and incoming.payload.get("status") == "candidate"
            and incoming.payload.get("deleted") is not True):
        raise OperationConflict("The candidate phase was removed. Update the client before generating cards.")
    # Current proposals are reconciled from canonical source; a client may only Dismiss.
    dismissal = (
        _dismissed_proposal(session, incoming)
        if incoming.entity_type == "proposal" and provenance is None else None
    )
    if incoming.entity_type == "proposal_event":
        if provenance is None and incoming.payload.get("authority") != "local_source":
            raise OperationConflict("canonical proposal history is server-owned")
        prior = session.get(Entity, ("proposal_event", "proposals", incoming.entity_id))
        if prior is not None and prior.payload != incoming.payload:
            raise OperationConflict("proposal history events are immutable")
    _validate_logical_comment_operation(session, incoming, provenance)
    if incoming.entity_type == "review_event":
        from .review_retirement import retire_event_error
        if (retire_error := retire_event_error(incoming.payload)) is not None:
            raise OperationConflict(retire_error)
        # Knowledge cards and project cards (protocol/review-card-kinds.md section 9).
        from .review_kinds import kind_event_error
        if (kind_error := kind_event_error(incoming.payload)) is not None:
            raise OperationConflict(kind_error)
    if incoming.entity_type == "review_card" and incoming.payload.get("deleted") is not True:
        from .review_kinds import card_record_error
        if (kind_error := card_record_error(incoming.payload)) is not None:
            raise OperationConflict(kind_error)
    identity_fields = {
        "agent_conversation": ("link_id",),
        "annotation": ("annotation_id",),
        "comment": ("id", "annotation_id", "comment_key"),
        "document_feedback": ("feedback_id",),
        "logical_comment": ("comment_id",),
        "pdf_link_override": ("override_id",),
        "proposal": ("proposal_id",),
        "proposal_event": ("event_id",),
        "review_correspondence_review": ("review_key",),
        "resolution": ("id", "annotation_id", "comment_key"),
        "review_card": ("card_id",),
        "review_card_feedback": ("feedback_id",),
        "review_event": ("event_id",),
        "review_exclusion": ("exclusion_id",),
        "review_tag_groups": ("layout_id",),
        "review_tag_merge": ("merge_id",),
    }
    payload_id = next(
        (
            incoming.payload.get(field)
            for field in identity_fields[incoming.entity_type]
            if incoming.payload.get(field)
        ),
        None,
    )
    if payload_id is not None and str(payload_id) != incoming.entity_id:
        raise OperationConflict("payload identity does not match entity_id")

    projected_type = projection_entity_type(incoming.entity_type)
    if incoming.entity_type == "logical_comment":
        entity = session.scalar(
            select(Entity)
            .where(
                Entity.entity_type == projected_type,
                Entity.collection == incoming.collection,
                Entity.entity_id == incoming.entity_id,
            )
            .with_for_update()
        )
    else:
        entity = session.get(Entity, (projected_type, incoming.collection, incoming.entity_id))
    restore_blocked = False
    if entity is not None and entity.deleted and incoming.payload.get("deleted") is not True:
        from .document_deletion import deletion_blocks_restore
        owner = entity
        if projected_type == "annotation":
            logical = session.get(Entity, ("logical_comment", "comments", incoming.entity_id))
            if logical is not None and logical.deleted:
                owner = logical
        if deletion_blocks_restore(session, owner):
            if incoming.entity_type != "pdf_link_override":
                raise OperationConflict("The owning document was deleted; restore the document before restoring this item")
            # A link override edited offline after its PDF was deleted loses to the
            # cascade tombstone without blocking the client's other operations.
            restore_blocked = True
    # A cascade already achieved this client deletion; retain its receipt without
    # overwriting the canonical tombstone or requiring its old base revision.
    already_deleted_reply = (
        incoming.entity_type == "logical_comment" and provenance is None
        and entity is not None and entity.deleted
        and incoming.payload.get("deleted") is True
        and bool(incoming.payload.get("parent_id"))
    )
    entity_payload = incoming.payload
    if incoming.entity_type == "logical_comment" and not already_deleted_reply:
        current_revision = entity.revision if entity is not None else 0
        source_only_advance = False
        if (entity is not None and not entity.deleted
                and incoming.base_revision is not None and 0 < incoming.base_revision < current_revision):
            advances = list(session.scalars(select(Operation).where(
                Operation.entity_type == "logical_comment", Operation.collection == "comments",
                Operation.entity_id == incoming.entity_id, Operation.outcome == "applied",
                Operation.entity_revision > incoming.base_revision)))
            # Server-owned source transitions do not invalidate an offline edit or
            # delete. A content advance needs a client-managed comment; an
            # anchor-removed transition only changed the server-owned scope.
            client_managed = entity.payload.get("source_authority") == "client-v1"
            source_only_advance = bool(advances) and all(
                (operation.provenance or "").startswith("canonical-source-comment-anchor:")
                or (client_managed and (operation.provenance or "").startswith("canonical-source-comment-content:"))
                for operation in advances)
        if incoming.base_revision != current_revision and not source_only_advance:
            raise OperationConflict(
                "logical comment revision conflict: "
                f"expected base_revision {current_revision}, received {incoming.base_revision}"
            )
        proposed_revision = current_revision + 1
        from .comment_records import comment_record
        entity_payload = comment_record(incoming.payload, incoming.payload)
        entity_payload["revision"] = proposed_revision
        from .logical_comments import MUTABLE_COMMENT_FIELDS
        previous_payload = entity.payload if entity is not None else {}
        content_changed = not previous_payload or any(
            previous_payload.get(field) != entity_payload.get(field) for field in MUTABLE_COMMENT_FIELDS
        )
        entity_payload["content_updated_at"] = (
            entity_payload.get("updated_at", incoming.client_timestamp) if content_changed
            else previous_payload.get("content_updated_at") or previous_payload.get("updated_at")
                 or previous_payload.get("created_at")
        )
        if previous_payload.get("created_at"):
            entity_payload["created_at"] = previous_payload["created_at"]
        if entity is not None and provenance is None:
            entity_payload["scope"] = entity.payload.get("scope", {})
            if entity_payload.get("subtype") != "Note" and "linking" in entity.payload:
                entity_payload["linking"] = entity.payload["linking"]
            else:
                entity_payload.pop("linking", None)
        if previous_payload.get("source_authority") == "client-v1":
            entity_payload["source_authority"] = "client-v1"
        if object_store is not None:
            from .source_comment_lifecycle import canonical_source_comment
            entity_payload = canonical_source_comment(session, object_store, entity_payload)
    elif dismissal is not None:
        entity_payload = dismissal
        proposed_revision = dismissal["revision"]
    else:
        proposed_revision = _integer_revision(
            incoming.payload,
            (entity.revision + 1) if entity else 1,
        )
    rebased_link = (
        _rebased_manual_source_link(session, incoming, entity)
        if incoming.entity_type == "review_card" and provenance is None else None
    )
    if rebased_link is not None:
        entity_payload = rebased_link
        proposed_revision = entity.revision + 1
    feedback_kept_fields = False
    immutable_fields = IMMUTABLE_FIELDS_BY_FEEDBACK_TYPE.get(incoming.entity_type)
    if immutable_fields is not None and entity is not None:
        # protocol/review-card-feedback.md and document-feedback.md: the passage, card and
        # creation facts are fixed by the first write; later writes only change polarity,
        # comment, deletion.
        fixed = {key: entity.payload[key] for key in immutable_fields if key in entity.payload}
        if any(incoming.payload.get(key) != value for key, value in fixed.items()):
            entity_payload = {**incoming.payload, **fixed}
            feedback_kept_fields = True
    outcome = "applied"
    should_apply = entity is None
    if entity is not None:
        if incoming.entity_type == "logical_comment":
            should_apply = not already_deleted_reply
        elif dismissal is not None:
            should_apply = True
        elif incoming.entity_type in {"review_event", "review_exclusion", "proposal_event", "review_tag_merge"}:
            # Immutable records: the first write is kept, later ones are superseded.
            should_apply = False
        elif restore_blocked:
            should_apply = False
        elif incoming.entity_type in IMMUTABLE_FIELDS_BY_FEEDBACK_TYPE:
            # Delete is sticky in every arrival order: at equal revision a delete beats a
            # non-delete whatever the times, so only Undo (a revision above the delete)
            # revives a highlight. protocol/review-card-feedback.md section 5.
            incoming_priority = _priority(incoming, proposed_revision)
            stored_priority = _stored_priority(entity)
            should_apply = (
                (incoming_priority[0], incoming.payload.get("deleted") is True, *incoming_priority[1:])
                > (stored_priority[0], entity.deleted, *stored_priority[1:])
            )
        else:
            should_apply = _priority(incoming, proposed_revision) > _stored_priority(entity)
        if not should_apply:
            outcome = "superseded"

    operation = Operation(
        operation_id=incoming.operation_id,
        device_id=device_id,
        device_sequence=incoming.device_sequence,
        entity_type=incoming.entity_type,
        entity_id=incoming.entity_id,
        collection=incoming.collection,
        base_revision=incoming.base_revision,
        client_timestamp=incoming.client_timestamp,
        payload=({**entity_payload, "_source_submission_sha256": hashlib.sha256(
            json.dumps(incoming.payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            if incoming.entity_type == "logical_comment" and entity_payload.get("source_authority") == "client-v1"
            # Keep the submission so a retry of the same operation stays a duplicate.
            else incoming.payload if rebased_link is not None or dismissal is not None or feedback_kept_fields
            else entity_payload),
        outcome=outcome,
        entity_revision=proposed_revision if should_apply else entity.revision,
        provenance=provenance,
    )
    session.add(operation)
    session.flush()
    anchor_job = None
    if should_apply:
        if incoming.entity_type in {"annotation", "comment"}:
            entity_payload = dict(incoming.payload)
            if provenance is None:
                entity_payload["logical_revision"] = proposed_revision
        deleted = entity_payload.get("deleted") is True
        if entity is None:
            entity = Entity(
                entity_type=projected_type,
                entity_id=incoming.entity_id,
                collection=incoming.collection,
                revision=proposed_revision,
                deleted=deleted,
                winner_operation_id=incoming.operation_id,
                payload=entity_payload,
            )
            session.add(entity)
        else:
            entity.revision = proposed_revision
            entity.collection = incoming.collection
            entity.deleted = deleted
            entity.winner_operation_id = incoming.operation_id
            entity.payload = entity_payload
            entity.updated_at = utc_now()
        if (
            incoming.entity_type in {"annotation", "comment"}
            and provenance is None
            and (
                incoming.payload.get("discussion") is True
                or "contents" in incoming.payload
            )
            and not (
                incoming.payload.get("in_reply_to")
                or incoming.payload.get("parent_id")
            )
        ):
            publication = session.get(Publication, f"{incoming.collection}.pdf")
            logical = session.get(Entity, ("logical_comment", "comments", incoming.entity_id))
            from .logical_comments import is_anchor_removed
            # A legacy placement push must not re-anchor a removed marker from
            # PDF geometry; restoring it is a source edit.
            if (publication is not None and publication.project.strip()
                    and not (logical is not None and is_anchor_removed(logical.payload.get("scope")))):
                anchor_job = AnnotationAnchorJob(
                    id=str(uuid.uuid4()),
                    annotation_id=incoming.entity_id,
                    collection=incoming.collection,
                    input_operation_id=incoming.operation_id,
                    input_revision=proposed_revision,
                    status="queued",
                )
                session.add(anchor_job)
        if incoming.entity_type == "logical_comment" and provenance is None:
            from .logical_comments import (
                project_logical_comment_to_origin,
                tombstone_thread_replies,
            )

            projected_anchor_job_id = project_logical_comment_to_origin(
                session,
                incoming,
                entity,
                object_store,
            )
            if projected_anchor_job_id is not None:
                anchor_job = session.get(AnnotationAnchorJob, projected_anchor_job_id)
            if entity.deleted and not entity.payload.get("parent_id"):
                tombstone_thread_replies(
                    session,
                    entity,
                    source_operation_id=incoming.operation_id,
                )
        if (incoming.entity_type == "review_correspondence_review" and provenance is None
                and incoming.payload.get("action") == "recompute"
                and incoming.entity_id.startswith("comment:")):
            from .logical_comments import project_logical_comment_to_origin

            comment_id = incoming.entity_id.removeprefix("comment:")
            logical = session.get(Entity, ("logical_comment", "comments", comment_id))
            if logical is not None and not logical.deleted:
                # Re-project only placement state under a new operation identity.
                # The comment revision and the user's offline edits are untouched.
                retry_id = project_logical_comment_to_origin(session, incoming, logical, object_store)
                if retry_id is not None:
                    anchor_job = session.get(AnnotationAnchorJob, retry_id)
    if (
        incoming.entity_type in {"annotation", "comment"}
        and provenance is None
        and (
            should_apply
            or (
                incoming.payload.get("deleted") is True
                and incoming.payload.get("source_anchor_deletion") is not True
            )
        )
    ):
        from .logical_comments import shadow_legacy_annotation

        shadow_legacy_annotation(session, incoming, entity)
    session.flush()
    return OperationResult(
        operation_id=incoming.operation_id,
        server_sequence=operation.server_sequence,
        outcome=outcome,
        entity_revision=entity.revision if entity else proposed_revision,
        anchor_job_id=anchor_job.id if should_apply and anchor_job is not None else None,
    )


def apply_batch(
    session: Session,
    device_id: str,
    operations: list[OperationInput],
    object_store: ObjectStore | None = None,
    review_source_settings=None,
    marker_removal_settings=None,
) -> list[OperationResult]:
    if len({operation.operation_id for operation in operations}) != len(operations):
        raise OperationConflict("batch contains duplicate operation IDs")
    if len({operation.device_sequence for operation in operations}) != len(operations):
        raise OperationConflict("batch contains duplicate device sequences")
    with ExitStack() as locks:
        try:
            results = []
            placement_updates = set()
            restored_cards = set()
            needs_source_links = False
            for operation in operations:
                note_only = False
                previous_card_payload = None
                if operation.entity_type == "review_card":
                    existing_card = session.get(Entity, ("review_card", operation.collection, operation.entity_id))
                    if existing_card is not None and not existing_card.deleted:
                        previous_card_payload = copy.deepcopy(existing_card.payload)
                        metadata = {"note", "revision", "updated_at", "origin_id"}
                        note_only = (
                            operation.payload.get("note", "") != existing_card.payload.get("note", "")
                            and {k: v for k, v in operation.payload.items() if k not in metadata}
                            == {k: v for k, v in existing_card.payload.items() if k not in metadata}
                        )
                    if operation.payload.get("move") is not None:
                        from .review_card_move import validate_card_move
                        validate_card_move(session, object_store, operation, previous_card_payload)
                    from .html_attachment_cards import check_card
                    check_card(session, object_store, operation, previous_card_payload)
                result = apply_operation(session, device_id, operation, object_store=object_store,
                                         allow_source_delete=review_source_settings is not None)
                if operation.payload.get("source_change") and result.outcome == "superseded":
                    from .client_review_sources import apply_client_review_source
                    apply_client_review_source(session, object_store,
                        review_source_settings or marker_removal_settings, operation, locks, already_applied_only=True)
                if operation.payload.get("source_authority") == "client-v1" and result.outcome == "applied" and operation.entity_type in {"review_card", "review_event"}:
                    from .client_review_sources import apply_client_review_source
                    apply_client_review_source(session, object_store,
                        review_source_settings or marker_removal_settings, operation, locks,
                        previous_payload=previous_card_payload)
                elif "delete_source" in operation.payload and result.outcome == "applied":
                    from .review_source_delete import apply_review_source_delete

                    apply_review_source_delete(session, object_store, review_source_settings,
                                               device_id, operation, locks)
                elif (marker_removal_settings is not None and result.outcome == "applied"
                      and operation.entity_type == "review_event"
                      and operation.payload.get("rating") == "delete"):
                    from .review_source_delete import apply_review_source_delete

                    apply_review_source_delete(session, object_store, marker_removal_settings,
                                               device_id, operation, locks, delete_text=False)
                if result.outcome == "applied" and operation.entity_type in {
                        "review_card", "review_event", "proposal"}:
                    # A card deleted, moved or linked another way, or a dismissed proposal, drops the
                    # card move waiting for that proposal (pending_card_moves.py).
                    from .pending_card_moves import after_operation
                    after_operation(session, operation)
                if (result.outcome == "applied" and operation.entity_type == "review_event"
                        and operation.payload.get("queue_action") in {"restore", "undo_retire"}):
                    # A restore, or an undo of a retire, may make a retired card active again.
                    restored_cards.add(operation.payload.get("card_id"))
                if operation.entity_type == "review_card" and result.outcome == "applied" and not note_only:
                    from .html_attachment_cards import is_attachment_card
                    # A card made in an HTML attachment is never linked to source markers.
                    needs_source_links |= not is_attachment_card(operation.payload) and (
                        operation.payload.get("source_authority") != "client-v1"
                        or not (operation.payload.get("primary") or {}).get("source_anchor"))
                    if isinstance(operation.payload.get("primary", {}).get("source_anchor"), dict):
                        placement_updates.add(operation.entity_id)
                results.append(result)
            if needs_source_links:
                from .review_source_link import queue_source_links
                queue_source_links(session, object_store)
            if object_store is not None:
                from .review_reanchor import place_restored_cards, project_updated_source_cards
                project_updated_source_cards(session, object_store, placement_updates)
                place_restored_cards(session, object_store, restored_cards)
            session.commit()
            return results
        except Exception:
            session.rollback()
            raise
