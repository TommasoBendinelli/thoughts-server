# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Verify render-neutral review metadata edits without relabeling build proofs."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from functools import lru_cache

from .proposal_syntax import parse_tex, tex_wrappers
from .renderer_tools.review_source_staging import COMMAND, SourceScanner, group


@lru_cache(maxsize=128)
def metadata_ranges(raw: bytes) -> list[tuple[int, int]]:
    text = raw.decode("utf-8")
    scanner = SourceScanner(text)
    scanner.scan()
    ranges = []
    for match in COMMAND.finditer(text):
        if match[1] not in {"CommentHighlight", "CommentStrikethrough", "CommentAnchor", "ReviewAnchor"}:
            continue
        if any(a < match.start() < b for a, b in scanner.hidden):
            continue
        line = text[text.rfind("\n", 0, match.start()) + 1 : match.start()]
        if "%" in line:  # Conservative; escaped percent may cause an extra rebuild.
            continue
        first = group(text, match.end(), len(text))
        second = group(text, first[3], len(text)) if first else None
        if second:
            ranges.append((len(text[: second[1]].encode()), len(text[: second[2]].encode())))
    # Proposal metadata and explanations never render. Live wrappers come from the
    # shared proposal lexer (spec section 4.2), broken ones included: TeX reads them too.
    for wrapper in tex_wrappers(text):
        if wrapper.metadata is not None:
            ranges.append((len(text[: wrapper.name_end].encode()), len(text[: wrapper.metadata[1]].encode())))
        if len(wrapper.groups) >= 2:
            explanation = wrapper.groups[1]
            ranges.append(
                (len(text[: explanation.content_start].encode()), len(text[: explanation.content_end].encode()))
            )
    return sorted(ranges)


def refresh_ranges(raw: bytes, accepted: frozenset[str] = frozenset()) -> list[tuple[int, int]]:
    """Exclude only wrappers whose removal is verified against the current source."""
    ranges = metadata_ranges(raw)
    if accepted:
        text = raw.decode("utf-8")
        for proposal in parse_tex(text).proposals:
            if proposal.id not in accepted:
                continue
            for start, end in (
                (proposal.start, proposal.replacement_start),
                (proposal.replacement_end, proposal.end),
            ):
                ranges = [*ranges, (len(text[:start].encode()), len(text[:end].encode()))]
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def metadata_form(raw: bytes, accepted: frozenset[str] = frozenset()) -> bytes:
    output, cursor = [], 0
    for start, end in refresh_ranges(raw, accepted):
        output.append(raw[cursor:start])
        cursor = end
    output.append(raw[cursor:])
    return b"".join(output)


def metadata_equivalent(before: bytes, after: bytes) -> bool:
    try:
        return metadata_form(before) == metadata_form(after)
    except (UnicodeDecodeError, ValueError):
        return False


def map_offset(offset: int, before: bytes, after: bytes, accepted: frozenset[str] = frozenset()) -> int:
    removed = 0
    for start, end in refresh_ranges(before, accepted):
        if start < offset < end:
            raise ValueError("source target is inside changed review metadata")
        if end <= offset:
            removed += end - start
    canonical = offset - removed
    added = 0
    for start, end in refresh_ranges(after):
        if start - added <= canonical:
            added += end - start
    return canonical + added


def metadata_refresh(manifest: dict, current: dict[str, bytes], read_object) -> dict | None:
    """Keep the original geometry and attach an explicit source revision mapping.

    None means a target, rendered byte, or input set changed: publication must
    wait for a real build. Logical comment operations can still synchronize.
    """
    rendered = manifest.get("placement_sources", manifest["sources"])
    from .renderer_tools.publish_phone_source_bundle import (
        parse_agent_proposals,
        parse_review_card_anchors,
    )

    placement_proposals = manifest.get("placement_agent_proposals", manifest.get("agent_proposals", []))
    placement_correspondences = manifest.get(
        "placement_review_correspondences", manifest.get("review_correspondences", [])
    )
    parsed_proposals = {
        value["id"]: value for path, raw in current.items() for value in parse_agent_proposals(path, raw)
    }
    original_ids = {value["id"] for value in placement_proposals}
    if not set(parsed_proposals).issubset(original_ids):
        return None  # New proposals need their own measured placement.
    accepted = frozenset(original_ids - set(parsed_proposals))
    originals, mappings = {}, []
    for source in rendered:
        path = source["path"]
        if path not in current:
            return None
        old = read_object(source["object"])
        if hashlib.sha256(old).hexdigest() != source["sha256"]:
            raise ValueError("immutable placement source object failed verification")
        new = current[path]
        if metadata_form(old, accepted) != metadata_form(new):
            return None
        originals[path] = old
        new_sha = hashlib.sha256(new).hexdigest()
        if new_sha != source["sha256"]:
            mappings.append(
                {
                    "source_file": path,
                    "from_sha256": source["sha256"],
                    "to_sha256": new_sha,
                    "method": "proposal_accept_metadata_v1" if accepted else "review_metadata_only_v1",
                    "render_input_sha256": hashlib.sha256(metadata_form(old, accepted)).hexdigest(),
                    "from_excluded_ranges": [list(pair) for pair in refresh_ranges(old, accepted)],
                    "to_excluded_ranges": [list(pair) for pair in metadata_ranges(new)],
                }
            )
    replacement = deepcopy(manifest)
    replacement["placement_sources"] = rendered
    if accepted or "placement_agent_proposals" in manifest:
        replacement["placement_agent_proposals"] = deepcopy(placement_proposals)
        replacement["placement_review_correspondences"] = deepcopy(placement_correspondences)
    replacement["agent_proposals"] = [deepcopy(p) for p in placement_proposals if p["id"] not in accepted]
    replacement["review_correspondences"] = [
        deepcopy(c)
        for c in placement_correspondences
        if c.get("review_key") not in {"proposal:" + identifier for identifier in accepted}
    ]
    replacement["source_revision_equivalences"] = mappings
    replacement["sources"] = [
        {
            **source,
            "sha256": hashlib.sha256(current[source["path"]]).hexdigest(),
            "object": hashlib.sha256(current[source["path"]]).hexdigest(),
            "bytes": len(current[source["path"]]),
        }
        for source in manifest["sources"]
    ]
    if "review_card_anchors" in manifest:
        placement_anchors = manifest.get(
            "placement_review_card_anchors", manifest["review_card_anchors"]
        )
        old_by_key = {}
        current_by_key = {}
        for source in rendered:
            path = source["path"]
            for raw, destination in (
                (originals[path], old_by_key),
                (current[path], current_by_key),
            ):
                for anchor in parse_review_card_anchors(path, raw):
                    key = (anchor["card_id"], anchor["segment_id"], path)
                    if key in destination:
                        return None
                    destination[key] = anchor
        refreshed_anchors = []
        placement_keys = [
            (anchor["card_id"], anchor["segment_id"], anchor["source_file"])
            for anchor in placement_anchors
        ]
        if len(placement_keys) != len(set(placement_keys)):
            return None
        for anchor in placement_anchors:
            key = (anchor["card_id"], anchor["segment_id"], anchor["source_file"])
            old = old_by_key.get(key)
            new = current_by_key.get(key)
            if old is None or new is None or any(
                anchor.get(field) != old[field]
                for field in old if field != "placement"
            ):
                return None
            refreshed_anchors.append({
                **anchor,
                **{field: value for field, value in new.items() if field != "placement"},
            })
        replacement["placement_review_card_anchors"] = deepcopy(placement_anchors)
        replacement["review_card_anchors"] = refreshed_anchors
    for proposal in replacement.get("agent_proposals", []):
        current_proposal = parsed_proposals.get(proposal.get("id"))
        if current_proposal is None:
            return None
        for key, value in current_proposal.items():
            if key not in {"placement", "pdf_filenames"}:
                proposal[key] = value
        proposal["source_sha256"] = hashlib.sha256(current[proposal["source_file"]]).hexdigest()
    for correspondence in replacement.get("review_correspondences", []):
        updated = []
        for target in correspondence.get("source_targets", []):
            path = target["source_file"]
            if path not in originals:
                return None
            new = current[path]
            mapped = deepcopy(target)
            for field in ("byte_start", "byte_end"):
                mapped[field] = map_offset(target[field], originals[path], new, accepted)
            for edge in ("start", "end"):
                prefix = new[: mapped["byte_" + edge]].decode()
                mapped["line_" + edge] = prefix.count("\n") + 1
                mapped["column_" + edge] = len(prefix.rsplit("\n", 1)[-1]) + 1
            mapped["source_sha256"] = hashlib.sha256(new).hexdigest()
            mapped["text_sha256"] = hashlib.sha256(new[mapped["byte_start"] : mapped["byte_end"]]).hexdigest()
            updated.append(mapped)
        correspondence["current_source_targets"] = updated
    return replacement


def validate_metadata_manifest(manifest: dict, read_object) -> None:
    if not manifest.get("source_revision_equivalences") and not manifest.get("placement_sources"):
        return
    current = {}
    for source in manifest["sources"]:
        raw = read_object(source["object"])
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError("current source revision object failed verification")
        current[source["path"]] = raw
    expected = metadata_refresh(manifest, current, read_object)
    if expected is None or expected["source_revision_equivalences"] != manifest.get(
        "source_revision_equivalences"
    ):
        raise ValueError("source revision equivalence is not render-neutral")
    if expected["agent_proposals"] != manifest.get("agent_proposals", []):
        raise ValueError("active proposals do not match the verified source revision")
    if "placement_review_card_anchors" in manifest and (
        expected["placement_review_card_anchors"] != manifest["placement_review_card_anchors"]
        or expected["review_card_anchors"] != manifest.get("review_card_anchors")
    ):
        raise ValueError("review card anchors do not match the verified source revision")
    for actual, wanted in zip(
        manifest.get("review_correspondences", []), expected.get("review_correspondences", []), strict=True
    ):
        if actual.get("current_source_targets") != wanted.get("current_source_targets"):
            raise ValueError("current source target does not match the verified revision mapping")
