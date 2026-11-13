# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from .renderer_tools.publish_phone_source_bundle import (
    BundleError,
    parse_agent_proposals,
    parse_review_comments,
)

PROVENANCE = re.compile(
    r"originating\s+Codex\s+conversation\s+hash\s*:\s*"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
GENERIC_COMMENT = re.compile(r"^Add this agent-authored block for review\.?$", re.IGNORECASE)


@dataclass
class ReviewCorrespondenceMigrationReport:
    source_files: int = 0
    proposals: int = 0
    review_anchors: int = 0
    explicit_review_anchors: int = 0
    anchors_to_formalize: int = 0
    delete_add_pairs: int = 0
    exact_noops: int = 0
    missing_provenance: int = 0
    generic_comments: int = 0
    duplicate_ids: int = 0
    parse_errors: int = 0
    modified_files: int = 0
    unresolved_artifact_items: int = 0
    batch_id: str = ""
    committed: bool = False
    aliases: list[dict[str, str]] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_files": self.source_files,
            "proposals": self.proposals,
            "review_anchors": self.review_anchors,
            "explicit_review_anchors": self.explicit_review_anchors,
            "anchors_to_formalize": self.anchors_to_formalize,
            "delete_add_pairs": self.delete_add_pairs,
            "exact_noops": self.exact_noops,
            "missing_provenance": self.missing_provenance,
            "generic_comments": self.generic_comments,
            "duplicate_ids": self.duplicate_ids,
            "parse_errors": self.parse_errors,
            "modified_files": self.modified_files,
            "unresolved_artifact_items": self.unresolved_artifact_items,
            "batch_id": self.batch_id,
            "committed": self.committed,
            "aliases": self.aliases,
            "quarantined": self.quarantined,
            "results": self.results,
            "strict_cutover_eligible": (
                self.parse_errors == 0
                and self.duplicate_ids == 0
                and self.unresolved_artifact_items == 0
                and not self.quarantined
            ),
        }


@dataclass
class PairBuildValidationReport:
    candidates: int = 0
    verified: int = 0
    verified_groups: int = 0
    approvals: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "candidates": self.candidates,
            "verified": self.verified,
            "verified_groups": self.verified_groups,
            "approvals": self.approvals,
            "groups": self.groups,
            "failures": self.failures,
        }


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ignorable_gap(raw: bytes) -> bool:
    return not re.sub(rb"(?m)%[^\n]*(?:\n|$)", b"", raw).strip()


def _content(raw: bytes, proposal: dict[str, Any], role: str) -> bytes:
    span = proposal[role]
    return raw[int(span["byte_start"]) : int(span["byte_end"])]


def _empty(proposal: dict[str, Any], role: str) -> bool:
    span = proposal[role]
    return int(span["byte_start"]) == int(span["byte_end"])


def _merged_proposal(raw: bytes, deletion: dict[str, Any], addition: dict[str, Any]) -> bytes:
    before = _content(raw, deletion, "before")
    after = _content(raw, addition, "after")
    comment = str(addition.get("comment") or "").strip()
    migration_sentence = (
        f"Migration merged adjacent legacy deletion {deletion['id']} into this atomic replacement."
    )
    combined_comment = f"{comment}\n{migration_sentence}" if comment else migration_sentence
    metadata = ""
    if addition.get("created_at"):
        metadata = "created=" + str(addition["created_at"])
        if addition.get("conversation_id"):
            metadata += ",conversation_id=" + str(addition["conversation_id"])
        if addition.get("conversation_url"):
            metadata += ",conversation_url=" + str(addition["conversation_url"])
        metadata = "[" + metadata + "]"
    return b"".join(
        (
            b"\\agentProposedEdit" + metadata.encode("utf-8") + b"{",
            str(addition["id"]).encode("utf-8"),
            b"}{",
            combined_comment.encode("utf-8"),
            b"}{",
            before,
            b"}{",
            after,
            b"}",
        )
    )


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_migration_report(
    report: ReviewCorrespondenceMigrationReport,
    path: Path,
) -> None:
    """Archive a deterministic, human-inspectable migration report."""

    _atomic_write(
        path.resolve(),
        (json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def _artifact_failures(docs_root: Path) -> list[dict[str, Any]]:
    roots = [
        docs_root / ".runtime" / "target" / "source-bundle",
        docs_root / ".runtime" / "source-bundle",
    ]
    root = next((candidate for candidate in roots if (candidate / "documents").is_dir()), None)
    if root is None:
        return []
    failures: dict[str, dict[str, Any]] = {}
    for pointer_path in sorted((root / "documents").glob("*.pdf.json")):
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            artifact_id = pointer["artifact_sha256"]
            raw = (root / "objects" / artifact_id).read_bytes()
            if _sha256(raw) != artifact_id:
                raise ValueError("artifact hash mismatch")
            manifest = json.loads(raw)
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            failures[str(pointer_path)] = {
                "review_key": str(pointer_path),
                "state": "needs_attention",
                "reason": f"invalid source artifact: {error}",
            }
            continue
        if manifest.get("review_correspondence_schema_version") != 2:
            continue
        for value in manifest.get("review_correspondences", []):
            if isinstance(value, dict) and value.get("state") in {"linking", "needs_attention"}:
                failures[str(value.get("review_key"))] = {
                    "review_key": value.get("review_key"),
                    "state": value.get("state"),
                    "reason": value.get("reason"),
                }
    return sorted(failures.values(), key=lambda value: str(value["review_key"]))


def _valid_build_hashes(item: dict[str, Any], *, require_variant_sources: bool) -> bool:
    names = ["source_sha256", "accept_pdf_sha256", "reject_pdf_sha256"]
    if require_variant_sources:
        names.extend(("accept_source_sha256", "reject_source_sha256"))
    return all(
        isinstance(item.get(name), str) and re.fullmatch(r"[0-9a-f]{64}", item[name])
        for name in names
    )


def _validated_pair_approvals(docs_root: Path) -> set[tuple[str, str, str, str]]:
    path = docs_root / "comments" / "review_correspondence_migration_approvals.json"
    if not path.is_file():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    if value.get("schema_version") not in {1, 2} or not isinstance(value.get("pairs"), list):
        return set()
    approvals = set()
    for item in value["pairs"]:
        if not isinstance(item, dict) or item.get("verified") is not True:
            continue
        fields = (
            item.get("source_file"),
            item.get("deletion_id"),
            item.get("addition_id"),
            item.get("source_sha256"),
        )
        if not all(isinstance(field, str) and field for field in fields):
            continue
        if not _valid_build_hashes(
            item, require_variant_sources=value.get("schema_version") == 2
        ):
            continue
        approvals.add(fields)
    if value.get("schema_version") == 2 and isinstance(value.get("groups"), list):
        for group in value["groups"]:
            if (
                not isinstance(group, dict)
                or group.get("verified") is not True
                or group.get("method") != "isolated_minimal_dependency_group_builds_v1"
                or not _valid_build_hashes(group, require_variant_sources=True)
                or not isinstance(group.get("source_file"), str)
                or not isinstance(group.get("pairs"), list)
            ):
                continue
            for pair in group["pairs"]:
                if not isinstance(pair, dict):
                    continue
                deletion_id = pair.get("deletion_id")
                addition_id = pair.get("addition_id")
                if not all(isinstance(value, str) and value for value in (deletion_id, addition_id)):
                    continue
                approvals.add(
                    (
                        group["source_file"],
                        deletion_id,
                        addition_id,
                        group["source_sha256"],
                    )
                )
    return approvals


def _pair_candidates(
    source_file: str,
    raw: bytes,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    proposals = sorted(parse_agent_proposals(source_file, raw), key=lambda value: value["macro_start"])
    candidates = []
    consumed: set[str] = set()
    for left, right in zip(proposals, proposals[1:], strict=False):
        if left["id"] in consumed or right["id"] in consumed:
            continue
        gap = raw[int(left["macro_end"]) : int(right["macro_start"])]
        if not _ignorable_gap(gap):
            continue
        if not (
            not _empty(left, "before")
            and _empty(left, "after")
            and _empty(right, "before")
            and not _empty(right, "after")
        ):
            continue
        candidates.append((left, right))
        consumed.update({str(left["id"]), str(right["id"])})
    return candidates


def _build_pair_variants(
    docs_root: Path,
    source_path: Path,
    deletion: dict[str, Any],
    addition: dict[str, Any],
) -> dict[str, str]:
    """Compile hash-bound accept and reject variants without touching authored source."""

    return _build_pair_group_variants(docs_root, source_path, [(deletion, addition)])


def _rewrite_pair_group(
    raw: bytes,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    accept: bool,
) -> bytes:
    replacements: list[tuple[int, int, bytes]] = []
    for deletion, addition in pairs:
        replacement = _merged_proposal(raw, deletion, addition) if accept else _content(
            raw, deletion, "before"
        )
        replacements.append(
            (int(deletion["macro_start"]), int(addition["macro_end"]), replacement)
        )
    ordered = sorted(replacements)
    if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:], strict=False)):
        raise ValueError("proposal group contains overlapping replacements")
    rewritten = raw
    for start, end, replacement in reversed(ordered):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten


def _build_pair_group_variants(
    docs_root: Path,
    source_path: Path,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, str]:
    """Prove a dependency group in immutable accepted and rejected source sandboxes."""

    from .native_renderer import _compile_source

    relative = source_path.relative_to(docs_root)
    if len(relative.parts) < 4 or relative.parts[1:3] != ("tex", "source"):
        raise ValueError("pair source is not a standalone project source")
    project_name = relative.parts[0]
    tex_name = relative.parts[-1]
    if len(relative.parts) != 4 or Path(tex_name).name != tex_name:
        raise ValueError("nested standalone sources are not supported by the native renderer")
    real_project = docs_root / project_name
    raw = source_path.read_bytes()
    accepted = _rewrite_pair_group(raw, pairs, accept=True)
    rejected = _rewrite_pair_group(raw, pairs, accept=False)
    with tempfile.TemporaryDirectory(prefix="thoughts-review-pair-proof.") as temporary_name:
        sandbox_root = Path(temporary_name)
        for child in docs_root.iterdir():
            if child.name == project_name:
                continue
            (sandbox_root / child.name).symlink_to(child, target_is_directory=child.is_dir())
        sandbox_project = sandbox_root / project_name
        sandbox_project.mkdir()
        for child in real_project.iterdir():
            if child.name in {"build", "pdf", "tex"}:
                continue
            (sandbox_project / child.name).symlink_to(child, target_is_directory=child.is_dir())
        shutil.copytree(real_project / "tex", sandbox_project / "tex", symlinks=True)
        sandbox_source = sandbox_project / "tex" / "source" / tex_name
        hashes = {}
        for state, rewritten in (("accept", accepted), ("reject", rejected)):
            sandbox_source.write_bytes(rewritten)
            shutil.rmtree(sandbox_project / "build", ignore_errors=True)
            shutil.rmtree(sandbox_project / "pdf", ignore_errors=True)
            build = _compile_source(sandbox_root, sandbox_project, tex_name)
            hashes[f"{state}_pdf_sha256"] = _sha256(build.pdf.read_bytes())
            hashes[f"{state}_source_sha256"] = _sha256(rewritten)
        return hashes


def validate_review_correspondence_pairs(
    docs_root: Path,
    *,
    write_approvals: bool,
) -> PairBuildValidationReport:
    """Prove legacy replacements, including minimal file-local dependency groups."""

    root = docs_root.resolve()
    report = PairBuildValidationReport()
    approvals_path = root / "comments" / "review_correspondence_migration_approvals.json"
    reusable_pairs: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    reusable_groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    if approvals_path.is_file():
        try:
            existing_value = json.loads(approvals_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            existing_value = {}
        if existing_value.get("schema_version") in {1, 2}:
            for item in existing_value.get("pairs", []):
                if (
                    not isinstance(item, dict)
                    or item.get("verified") is not True
                    or not _valid_build_hashes(item, require_variant_sources=True)
                ):
                    continue
                key = tuple(
                    str(item.get(name) or "")
                    for name in ("source_file", "deletion_id", "addition_id", "source_sha256")
                )
                reusable_pairs[key] = item
            for group in existing_value.get("groups", []):
                if (
                    not isinstance(group, dict)
                    or group.get("verified") is not True
                    or group.get("method") != "isolated_minimal_dependency_group_builds_v1"
                    or not _valid_build_hashes(group, require_variant_sources=True)
                    or not isinstance(group.get("pairs"), list)
                ):
                    continue
                for pair in group["pairs"]:
                    if not isinstance(pair, dict):
                        continue
                    key = (
                        str(group.get("source_file") or ""),
                        str(pair.get("deletion_id") or ""),
                        str(pair.get("addition_id") or ""),
                        str(group.get("source_sha256") or ""),
                    )
                    reusable_groups[key] = group
    sources = sorted(
        source
        for project in root.glob("docs-*")
        for authored_root in (project / "tex/source", project / "tex/fragments")
        if authored_root.is_dir()
        for source in authored_root.rglob("*.tex")
    )
    for source_path in sources:
        source_file = source_path.relative_to(root).as_posix()
        try:
            raw = source_path.read_bytes()
            candidates = _pair_candidates(source_file, raw)
        except (BundleError, OSError, UnicodeError, ValueError) as error:
            report.failures.append({"source_file": source_file, "reason": str(error)})
            continue
        source_failures: list[dict[str, str]] = []
        for deletion, addition in candidates:
            report.candidates += 1
            identity = {
                "source_file": source_file,
                "deletion_id": str(deletion["id"]),
                "addition_id": str(addition["id"]),
                "source_sha256": _sha256(raw),
            }
            identity_key = (
                identity["source_file"],
                identity["deletion_id"],
                identity["addition_id"],
                identity["source_sha256"],
            )
            if existing := reusable_pairs.get(identity_key):
                report.approvals.append(existing)
                report.verified += 1
                continue
            if existing_group := reusable_groups.get(identity_key):
                if existing_group not in report.groups:
                    report.groups.append(existing_group)
                    report.verified_groups += 1
                report.verified += 1
                continue
            try:
                proof = _build_pair_variants(root, source_path, deletion, addition)
            except (OSError, ValueError, RuntimeError) as error:
                source_failures.append({**identity, "reason": str(error)})
                continue
            approval = {
                **identity,
                **proof,
                "verified": True,
                "method": "isolated_native_accept_reject_builds_v1",
            }
            report.approvals.append(approval)
            report.verified += 1
        if source_failures and 2 <= len(candidates) <= 8:
            candidate_by_ids = {
                (str(deletion["id"]), str(addition["id"])): (deletion, addition)
                for deletion, addition in candidates
            }
            covered_failures: set[tuple[str, str]] = set()
            for failure in source_failures:
                failed_ids = (failure["deletion_id"], failure["addition_id"])
                if failed_ids in covered_failures:
                    continue
                target = candidate_by_ids[failed_ids]
                companions = [pair for pair in candidates if pair != target]
                selected: tuple[list[tuple[dict[str, Any], dict[str, Any]]], dict[str, str]] | None = (
                    None
                )
                for group_size in range(2, min(4, len(candidates)) + 1):
                    for companion_group in combinations(companions, group_size - 1):
                        proposed_group = sorted(
                            [target, *companion_group], key=lambda pair: pair[0]["macro_start"]
                        )
                        try:
                            group_proof = _build_pair_group_variants(
                                root, source_path, proposed_group
                            )
                        except (OSError, ValueError, RuntimeError):
                            continue
                        selected = (proposed_group, group_proof)
                        break
                    if selected is not None:
                        break
                if selected is None:
                    continue
                proposed_group, group_proof = selected
                group = {
                    "source_file": source_file,
                    "source_sha256": _sha256(raw),
                    "pairs": [
                        {
                            "deletion_id": str(deletion["id"]),
                            "addition_id": str(addition["id"]),
                        }
                        for deletion, addition in proposed_group
                    ],
                    **group_proof,
                    "verified": True,
                    "method": "isolated_minimal_dependency_group_builds_v1",
                }
                report.groups.append(group)
                report.verified_groups += 1
                group_ids = {
                    (pair["deletion_id"], pair["addition_id"]) for pair in group["pairs"]
                }
                newly_covered = {
                    (item["deletion_id"], item["addition_id"])
                    for item in source_failures
                    if (item["deletion_id"], item["addition_id"]) in group_ids
                } - covered_failures
                covered_failures.update(newly_covered)
                report.verified += len(newly_covered)
            report.failures.extend(
                failure
                for failure in source_failures
                if (failure["deletion_id"], failure["addition_id"]) not in covered_failures
            )
        else:
            report.failures.extend(source_failures)
    report.approvals.sort(
        key=lambda value: (
            value["source_file"],
            value["deletion_id"],
            value["addition_id"],
        )
    )
    report.groups.sort(key=lambda value: (value["source_file"], value["source_sha256"]))
    report.failures.sort(
        key=lambda value: (
            value.get("source_file", ""),
            value.get("deletion_id", ""),
            value.get("addition_id", ""),
        )
    )
    if write_approvals:
        existing: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        existing_groups: dict[tuple[str, str], dict[str, Any]] = {}
        if approvals_path.is_file():
            value = json.loads(approvals_path.read_text(encoding="utf-8"))
            if value.get("schema_version") not in {1, 2} or not isinstance(value.get("pairs"), list):
                raise ValueError("existing review correspondence pair approvals are invalid")
            for item in value["pairs"]:
                if not isinstance(item, dict):
                    raise ValueError("existing review correspondence pair approval is invalid")
                key = tuple(
                    str(item.get(name) or "")
                    for name in ("source_file", "deletion_id", "addition_id", "source_sha256")
                )
                existing[key] = item
            if value.get("schema_version") == 2:
                if not isinstance(value.get("groups"), list):
                    raise ValueError("existing review correspondence group approvals are invalid")
                for item in value["groups"]:
                    if not isinstance(item, dict):
                        raise ValueError("existing review correspondence group approval is invalid")
                    existing_groups[
                        (str(item.get("source_file") or ""), str(item.get("source_sha256") or ""))
                    ] = item
        for item in report.approvals:
            key = tuple(
                str(item[name])
                for name in ("source_file", "deletion_id", "addition_id", "source_sha256")
            )
            existing[key] = item
        for item in report.groups:
            existing_groups[(item["source_file"], item["source_sha256"])] = item
        _atomic_write(
            approvals_path,
            (
                json.dumps(
                    {
                        "schema_version": 2,
                        "pairs": sorted(
                            existing.values(),
                            key=lambda value: (
                                str(value.get("source_file") or ""),
                                str(value.get("deletion_id") or ""),
                                str(value.get("addition_id") or ""),
                                str(value.get("source_sha256") or ""),
                            ),
                        ),
                        "groups": sorted(
                            existing_groups.values(),
                            key=lambda value: (
                                str(value.get("source_file") or ""),
                                str(value.get("source_sha256") or ""),
                            ),
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )
    return report


def migrate_review_correspondence(
    docs_root: Path,
    *,
    commit: bool,
) -> ReviewCorrespondenceMigrationReport:
    """Normalize only deterministic legacy declarations and report every exception."""

    root = docs_root.resolve()
    report = ReviewCorrespondenceMigrationReport()
    pair_approvals = _validated_pair_approvals(root)
    originals: dict[Path, bytes] = {}
    replacements_by_file: dict[Path, list[tuple[int, int, bytes, str]]] = {}
    proposal_occurrences: dict[str, list[str]] = {}
    anchor_occurrences: dict[str, list[str]] = {}

    sources = sorted(
        source
        for project in root.glob("docs-*")
        for authored_root in (project / "tex/source", project / "tex/fragments")
        if authored_root.is_dir()
        for source in authored_root.rglob("*.tex")
    )
    report.source_files = len(sources)
    for source_path in sources:
        relative = source_path.relative_to(root).as_posix()
        try:
            raw = source_path.read_bytes()
            proposals = sorted(parse_agent_proposals(relative, raw), key=lambda value: value["macro_start"])
            anchors = parse_review_comments(relative, raw)
        except (BundleError, OSError, UnicodeError, ValueError) as error:
            report.parse_errors += 1
            report.quarantined.append({"source_file": relative, "reason": str(error)})
            continue
        originals[source_path] = raw
        report.proposals += len(proposals)
        report.review_anchors += len(anchors)
        report.explicit_review_anchors += sum(value.get("explicit_body") is True for value in anchors)
        for anchor in anchors:
            anchor_occurrences.setdefault(str(anchor["id"]), []).append(relative)
            if anchor.get("explicit_body") is True:
                continue
            report.anchors_to_formalize += 1
            offset = int(anchor["macro_end"])
            replacements_by_file.setdefault(source_path, []).append(
                (offset, offset, b"{}", f"formalize_anchor:{anchor['id']}")
            )

        for proposal in proposals:
            proposal_occurrences.setdefault(str(proposal["id"]), []).append(relative)
            comment = str(proposal.get("comment") or "").strip()
            if not PROVENANCE.search(comment):
                report.missing_provenance += 1
            if GENERIC_COMMENT.fullmatch(comment):
                report.generic_comments += 1

        consumed: set[str] = set()
        for left, right in zip(proposals, proposals[1:], strict=False):
            if left["id"] in consumed or right["id"] in consumed:
                continue
            gap = raw[int(left["macro_end"]) : int(right["macro_start"])]
            if not _ignorable_gap(gap):
                continue
            if not (
                not _empty(left, "before")
                and _empty(left, "after")
                and _empty(right, "before")
                and not _empty(right, "after")
            ):
                continue
            report.delete_add_pairs += 1
            if _content(raw, left, "before") == _content(raw, right, "after"):
                report.exact_noops += 1
                consumed.update({str(left["id"]), str(right["id"])})
                replacements_by_file.setdefault(source_path, []).append(
                    (
                        int(left["macro_start"]),
                        int(right["macro_end"]),
                        _content(raw, left, "before"),
                        f"archive_noop_pair:{left['id']}:{right['id']}",
                    )
                )
                continue
            approval_key = (relative, str(left["id"]), str(right["id"]), _sha256(raw))
            if approval_key not in pair_approvals:
                report.quarantined.append(
                    {
                        "source_file": relative,
                        "deletion_id": str(left["id"]),
                        "addition_id": str(right["id"]),
                        "source_sha256": _sha256(raw),
                        "reason": "atomic_pair_requires_accept_and_reject_build_validation",
                    }
                )
                continue
            consumed.update({str(left["id"]), str(right["id"])})
            report.aliases.append(
                {
                    "legacy_id": str(left["id"]),
                    "canonical_id": str(right["id"]),
                    "reason": "adjacent_delete_add_pair",
                }
            )
            replacements_by_file.setdefault(source_path, []).append(
                (
                    int(left["macro_start"]),
                    int(right["macro_end"]),
                    _merged_proposal(raw, left, right),
                    f"merge:{left['id']}:{right['id']}",
                )
            )

        for proposal in proposals:
            if proposal["id"] in consumed or proposal.get("kind") != "edit":
                continue
            if proposal["before"]["sha256"] != proposal["after"]["sha256"]:
                continue
            report.exact_noops += 1
            replacements_by_file.setdefault(source_path, []).append(
                (
                    int(proposal["macro_start"]),
                    int(proposal["macro_end"]),
                    _content(raw, proposal, "after"),
                    f"archive_noop:{proposal['id']}",
                )
            )

    duplicate_ids = {
        f"proposal:{identity}": paths
        for identity, paths in proposal_occurrences.items()
        if len(paths) > 1
    }
    duplicate_ids.update(
        {
            f"comment:{identity}": paths
            for identity, paths in anchor_occurrences.items()
            if len(paths) > 1
        }
    )
    report.duplicate_ids = len(duplicate_ids)
    report.quarantined.extend(
        {"review_key": identity, "reason": "duplicate_id", "source_files": paths}
        for identity, paths in sorted(duplicate_ids.items())
    )

    modified: dict[Path, bytes] = {}
    for source_path, replacements in replacements_by_file.items():
        raw = originals[source_path]
        ordered = sorted(replacements, key=lambda value: (value[0], value[1]))
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:], strict=False)):
            report.quarantined.append(
                {
                    "source_file": source_path.relative_to(root).as_posix(),
                    "reason": "overlapping_deterministic_rewrites",
                }
            )
            continue
        rewritten = raw
        for start, end, replacement, _reason in reversed(ordered):
            rewritten = rewritten[:start] + replacement + rewritten[end:]
        if rewritten != raw:
            modified[source_path] = rewritten
            report.results.append(
                {
                    "source_file": source_path.relative_to(root).as_posix(),
                    "before_sha256": _sha256(raw),
                    "after_sha256": _sha256(rewritten),
                    "changes": [value[3] for value in ordered],
                }
            )

    report.modified_files = len(modified)
    batch_payload = {
        "sources": report.results,
        "aliases": sorted(report.aliases, key=lambda value: value["legacy_id"]),
    }
    report.batch_id = _sha256(
        json.dumps(batch_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )[:24]
    artifact_failures = _artifact_failures(root)
    report.unresolved_artifact_items = len(artifact_failures)
    report.quarantined.extend(artifact_failures)

    if not commit:
        return report
    if report.parse_errors or report.duplicate_ids:
        raise ValueError("migration commit refused because source parsing or proposal identities are invalid")

    backup_root = root / "comments" / ".migration-backups" / report.batch_id
    if backup_root.exists():
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"existing migration backup is incomplete: {backup_root}")
    else:
        for source_path in modified:
            destination = backup_root / source_path.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)
        _atomic_write(
            backup_root / "manifest.json",
            (json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
    for source_path, rewritten in modified.items():
        current = source_path.read_bytes()
        if _sha256(current) != next(
            value["before_sha256"]
            for value in report.results
            if value["source_file"] == source_path.relative_to(root).as_posix()
        ):
            raise ValueError(f"source changed after migration planning: {source_path}")
        _atomic_write(source_path, rewritten)

    aliases_path = root / "comments" / "review_correspondence_aliases.json"
    existing = {}
    if aliases_path.is_file():
        existing_value = json.loads(aliases_path.read_text(encoding="utf-8"))
        existing = {
            value["legacy_id"]: value
            for value in existing_value.get("aliases", [])
            if isinstance(value, dict) and isinstance(value.get("legacy_id"), str)
        }
    for value in report.aliases:
        previous = existing.get(value["legacy_id"])
        if previous is not None and previous != value:
            raise ValueError(f"legacy alias conflicts with existing registry: {value['legacy_id']}")
        existing[value["legacy_id"]] = value
    _atomic_write(
        aliases_path,
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "migration_batch_id": report.batch_id,
                    "aliases": sorted(existing.values(), key=lambda value: value["legacy_id"]),
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8"),
    )
    report.committed = True
    return report
