# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Apply a client-authored marker edit and its card operation in one DB transaction.

Only object identities travel with the operation. Objects are immutable and uploaded
beforehand; unreferenced uploads are harmless if validation or the transaction fails.
"""
from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import func, select

from .models import Device, Entity, ObjectRecord, Operation, SourceFile
from .operations import OperationConflict, latest_device_sequence
from .review_source_delete import delete_marked_passages, segment_identities
from .schemas import SourceApplyRequest
from .sources import _apply_source, source_transaction_lock

AUTHORITY = "client-v1"
DEVICE = "thoughts-client-review-sources"


def _verified_source_version(session, source, digest):
    return _verified_source_sequence(session, source, digest) is not None


def _verified_source_sequence(session, source, digest):
    """When the server last produced this version of the source, or None if it never did."""
    produced = session.scalar(select(func.max(Operation.server_sequence)).where(
        Operation.entity_type == "source", Operation.entity_id == source.path,
        Operation.outcome.in_(["applied", "merged"]),
        Operation.payload["result_sha256"].as_string() == digest))
    if produced is None and source.sha256 == digest:
        return 0
    return produced


REPLACEMENT_OTHER_FILE = ("The card's passage is marked in another source file. "
                          "Place its areas in that file, or remove its markers there first")


def card_marker_anchor(path: str, raw: bytes, card_id: str) -> dict | None:
    """The v2 anchor of every marker pair this card has in one source file."""
    if path.endswith((".md", ".markdown")):
        from .markdown_source import MarkdownError, parse_markdown

        try:
            markers = parse_markdown(raw).markers
        except MarkdownError as error:
            raise OperationConflict(str(error)) from error
        segments = [{"segment_id": key.split(":")[3], "source_file": path,
                     "content_sha256": hashlib.sha256(raw[m["byte_start"]:m["byte_end"]]).hexdigest()}
                    for key, m in markers.items()
                    if key.startswith(f"review:{card_id}:answer:")]
    else:
        from .renderer_tools.publish_phone_source_bundle import parse_review_card_anchors

        segments = [{key: item[key] for key in ("segment_id", "source_file", "content_sha256")}
                    for item in parse_review_card_anchors(path, raw) if item["card_id"] == card_id]
    return {"version": 2, "segments": segments} if segments else None


def strip_card_markers(path: str, raw: bytes, card_id: str) -> bytes:
    """Remove every marker of one card, keeping the text; refuse unpaired markers."""
    anchor = card_marker_anchor(path, raw, card_id)
    if anchor is None:
        raise OperationConflict("The review passage or its markers changed. Sync and reopen the card")
    stripped = delete_marked_passages({path: raw}, card_id, anchor, delete_text=False)[path]
    if card_id.encode() in stripped:
        raise OperationConflict("The review passage or its markers changed. Sync and reopen the card")
    return stripped


def card_marker_ids(path: str, raw: bytes) -> set[str]:
    """The cards with markers in one source file."""
    if path.endswith((".md", ".markdown")):
        from .markdown_source import parse_markdown

        return {key.split(":")[1] for key in parse_markdown(raw).markers if key.startswith("review:")}
    from .renderer_tools.publish_phone_source_bundle import parse_review_card_anchors

    return {item["card_id"] for item in parse_review_card_anchors(path, raw) if item["card_id"]}


def with_removed_card_markers(path: str, base: bytes, removed: bytes, other: bytes) -> bytes | None:
    """When `removed` is `base` with only the markers of some cards taken out, `other` with the
    same markers taken out; otherwise None.

    A line merge sees a marker removal as an edit of every marked line, so it conflicts with any
    edit of those lines on the other side, although the two never disagree about the text.
    """
    if not path.endswith((".tex", ".md", ".markdown")) or len(removed) >= len(base):
        return None
    try:
        gone = card_marker_ids(path, base) - card_marker_ids(path, removed)
        expected = base
        for card_id in sorted(gone):
            expected = strip_card_markers(path, expected, card_id)
        if not gone or expected != removed:
            return None
        result = other
        for card_id in sorted(gone & card_marker_ids(path, other)):
            result = strip_card_markers(path, result, card_id)
        return result
    except Exception:  # Unparseable or unpaired markers: leave it to the line merge.
        return None


def apply_client_review_source(session, store, settings, incoming, locks, *, already_applied_only=False,
                               previous_payload=None):
    payload = incoming.payload
    change = payload.get("source_change")
    if change is None:
        if incoming.entity_type == "review_event" and payload.get("rating") == "delete":
            if payload.get("marker_removal") not in {"pending", "not_required"}:
                raise OperationConflict("Client deletion must include its source change or pending status")
        return
    if store is None or settings is None or not isinstance(change, dict):
        raise OperationConflict("Client review source updates are unavailable")
    deleting = incoming.entity_type == "review_event" and payload.get("rating") == "delete"
    if incoming.entity_type != "review_card" and not deleting:
        raise OperationConflict("Source change requires card creation or deletion")
    path = change.get("path")
    before_hash, after_hash = change.get("before_sha256"), change.get("after_sha256")
    base_hash = change.get("base_sha256", before_hash)
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (before_hash, after_hash, base_hash)):
        raise OperationConflict("Invalid source object identity")
    if not isinstance(path, str) or not path.endswith((".tex", ".md", ".markdown")):
        raise OperationConflict("Invalid review source path")
    # SourceApplyRequest also rejects traversal and noncanonical paths.
    transaction = change.get("transaction_id", incoming.operation_id)
    try:
        uuid.UUID(transaction)
    except (ValueError, TypeError, AttributeError) as error:
        raise OperationConflict("Invalid local review transaction identity") from error
    source_operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"client-review-source:{incoming.entity_type}:{incoming.entity_id}:{transaction}"))
    request = SourceApplyRequest(operation_id=source_operation_id, device_sequence=1,
        path=path, base_revision=0, base_sha256=base_hash,
        base_object_sha256=base_hash, content_sha256=after_hash,
        client_timestamp=incoming.client_timestamp)
    locks.enter_context(source_transaction_lock(session, "client-review-source-device"))
    prior = session.scalar(select(Operation).where(Operation.operation_id == source_operation_id))
    if prior is not None:
        recorded_base = prior.payload.get("client_base_sha256", prior.payload.get("base_sha256"))
        recorded_before = prior.payload.get("client_before_sha256", before_hash)
        recorded_after = prior.payload.get("client_after_sha256", prior.payload.get("incoming_sha256"))
        if (prior.entity_id != path or recorded_base != base_hash or recorded_before != before_hash
                or recorded_after != after_hash):
            raise OperationConflict("A local review transaction cannot change after delivery")
        return
    if already_applied_only:
        raise OperationConflict("The card changed concurrently; its source transaction remains pending")
    values = []
    for digest in (before_hash, after_hash):
        record = session.get(ObjectRecord, digest)
        if record is None or record.size_bytes > 4 * 1024 * 1024:
            raise OperationConflict("Upload the verified review source objects first")
        value = store.path(digest).read_bytes()
        if hashlib.sha256(value).hexdigest() != digest:
            raise OperationConflict("Review source object failed verification")
        value.decode("utf-8")
        values.append(value)
    before, after = values
    card_id = payload.get("card_id")
    card = session.get(Entity, ("review_card", "cards", card_id))
    primary = (card.payload if deleting and card else payload).get("primary", {})
    anchor = primary.get("source_anchor")
    identities = segment_identities(anchor)
    # Several passages of one card may be marked, but one operation edits one source file.
    if not identities or {p for p, _ in identities} != {path}:
        raise OperationConflict("A local card operation must mark passages of one source file")
    if deleting:
        expected = delete_marked_passages({path: before}, card_id, anchor,
                                         delete_text=change.get("delete_text") is True)[path]
        if expected != after:
            raise OperationConflict("Client deletion changed text outside the selected marker operation")
    else:
        unmarked_before = before
        if card_id.encode() in before:
            # Editing the areas of a linked card moves its markers: the source
            # without this card's old markers must equal the source without its
            # new ones. Only an existing card may do this, within one file.
            if previous_payload is None:
                raise OperationConflict("Card markers already existed before creation")
            unmarked_before = strip_card_markers(path, before, card_id)
        if previous_payload is not None:
            old_files = {segment.get("source_file") for segment in
                         ((previous_payload.get("primary") or {}).get("source_anchor") or {}).get("segments", [])
                         if isinstance(segment, dict)} - {path}
            for other in old_files:
                record = session.get(SourceFile, other) if isinstance(other, str) else None
                if (record is not None and not record.deleted
                        and card_id.encode() in store.path(record.object_sha256).read_bytes()):
                    raise OperationConflict(REPLACEMENT_OTHER_FILE)
        # Verify the enclosed text hash, then prove creation preserved the source.
        delete_marked_passages({path: after}, card_id, anchor, delete_text=True, allow_overlap=True)
        restored = delete_marked_passages({path: after}, card_id, anchor, delete_text=False)[path]
        if restored.rstrip(b"\r\n") != unmarked_before.rstrip(b"\r\n"):
            raise OperationConflict("Client creation changed source text beyond its markers"
                                    if unmarked_before is before else
                                    "Moving the card's areas changed source text beyond its markers")
    locks.enter_context(source_transaction_lock(session, path))
    source = session.get(SourceFile, path, populate_existing=True)
    if source is None or source.deleted:
        raise OperationConflict("The editable source is unavailable")
    # A card deleted while its file had edits the server never received (for example while the
    # file waited for a merge) has no verified base. Its own change was verified above to remove
    # only this card's markers, so the deletion removes them from the server's current version,
    # which needs no base: the client's unpublished text is never taken. A deletion that also
    # removes the passage's text still needs a verified base.
    unverified_delete = False
    if not _verified_source_version(session, source, base_hash):
        if not deleting or change.get("delete_text") is True:
            raise OperationConflict("The review source merge base is not a verified server revision; Sync the source before retrying")
        unverified_delete = True

    # Consecutive local card edits retain the same last-synchronized base, but
    # an earlier edit may already have published this operation's exact before
    # version (including earlier edits in this batch). Use that verified parent
    # so the merge applies only this marker change and preserves remote edits.
    # An unpublished local draft is never sufficient evidence for a new base.
    # The before version must also be at least as recent as the recorded base: a
    # client whose file went back to an older server revision made a local edit
    # relative to its base, and merging from that older revision would turn its
    # own edit into a conflict that no retry can resolve.
    if unverified_delete:
        current = store.path(source.object_sha256).read_bytes()
        if card_id.encode() not in current:
            return  # Nothing of the card is left on the server.
        merge_base = source.sha256
        stripped = store.put_bytes(session, strip_card_markers(path, current, card_id), "text/plain; charset=utf-8")
        request = request.model_copy(update={"content_sha256": stripped.sha256})
    else:
        before_sequence = _verified_source_sequence(session, source, before_hash)
        base_sequence = _verified_source_sequence(session, source, base_hash)
        merge_base = before_hash if before_sequence is not None and before_sequence >= base_sequence else base_hash

    if session.get(Device, DEVICE) is None:
        session.add(Device(id=DEVICE, name="Client review source transactions",
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), enabled=False))
        session.flush()
    request = request.model_copy(update={"base_revision": source.revision,
        "base_sha256": merge_base, "base_object_sha256": merge_base,
        "device_sequence": latest_device_sequence(session, DEVICE) + 1})
    result = _apply_source(session, store, settings.worktree_root, DEVICE, request,
                           already_locked=True, render_impact="pdf")
    if result.outcome not in {"applied", "merged", "duplicate"}:
        raise OperationConflict("Concurrent source edits conflict with the local card operation; both remain pending")
    # Keep the submitted identity distinct from the selected merge base so a
    # retry or later card update can still verify the original transaction.
    applied = session.scalar(select(Operation).where(Operation.operation_id == source_operation_id))
    applied.payload = {**applied.payload, "client_base_sha256": base_hash, "client_before_sha256": before_hash,
                       "client_after_sha256": after_hash}
    # A conflict-free textual merge must also preserve the intended marker state.
    merged = store.path(result.object_sha256).read_bytes()
    if deleting:
        if card_id.encode() in merged:
            raise OperationConflict("Concurrent edits retained the deleted card markers")
    else:
        # A concurrent edit may change the text between the new card's markers. The
        # card follows its markers, as after any later edit; publication refreshes
        # its passage. The client's own change was verified exactly above.
        delete_marked_passages({path: merged}, card_id, anchor, delete_text=False)
