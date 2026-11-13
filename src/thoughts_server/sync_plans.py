# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .source_resources import SourceResourceError, normalize_manifest_resources

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROPOSAL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
SOURCE_RESOURCE_CAPABILITY = "source_resource_v1"
PDF_DELTA_CAPABILITY = "pdf_delta_v1"
COMPONENT_PACKAGE_CAPABILITY = "component_package_v1"
# The reader trusts the server's evidence validation and never opens the evidence object.
MARKDOWN_EVIDENCE_OMITTED_CAPABILITY = "markdown_evidence_omitted_v1"


class SyncPlanError(ValueError):
    """A published source artifact cannot satisfy the requested reader profile."""


@dataclass(frozen=True)
class SourcePlanDependency:
    sha256: str
    role: str


def _optional_digest(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise SyncPlanError(f"The source artifact contains an invalid {field} identity")
    return value


def _safe_source_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        return False
    normalized = value.replace("\\", "/")
    return all(part not in ("", ".", "..") for part in normalized.split("/"))


def component_package_dependencies(
    manifest: dict[str, Any], read_object: Callable[[str], bytes]
) -> list[SourcePlanDependency]:
    """Verify every component reference before making it available to a reader."""
    pdf = manifest.get("pdf")
    if not isinstance(pdf, dict):
        return []
    reference = pdf.get("composition")
    if reference is None:
        if pdf.get("component_review") is not None:
            raise SyncPlanError("Component reviews have no composition index")
        return []
    if not isinstance(reference, dict) or not isinstance(reference.get("documents"), list):
        raise SyncPlanError("The composition index reference is invalid")
    composition_id = _optional_digest(reference.get("composition_object"), "composition object")
    if composition_id is None:
        raise SyncPlanError("The composition index has no object")

    def verified_json(object_id: str) -> dict[str, Any]:
        try:
            raw = read_object(object_id)
            if hashlib.sha256(raw).hexdigest() != object_id:
                raise ValueError("digest mismatch")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return value
        except (OSError, ValueError, TypeError) as error:
            raise SyncPlanError(f"Component object failed verification: {object_id}") from error

    composition = verified_json(composition_id)
    documents = reference["documents"]
    reviews = pdf.get("component_review")
    segments = pdf.get("segments")
    if (
        composition.get("kind") != "thoughts-project-composition"
        or composition.get("pdf_sha256") != pdf.get("pdf_sha256")
        or composition.get("filename") != manifest.get("filename")
        or composition.get("page_count") != pdf.get("page_count")
        or composition.get("documents") != documents
        or not isinstance(reviews, list)
        or not isinstance(segments, list)
        or not documents
        or len(documents) != len(segments)
        or len(documents) != len(reviews)
    ):
        raise SyncPlanError("The composition index does not match its publication")
    dependencies = [SourcePlanDependency(composition_id, "project_composition")]
    for document, review, segment in zip(documents, reviews, segments, strict=True):
        if not all(isinstance(value, dict) for value in (document, review, segment)):
            raise SyncPlanError("A component reference is invalid")
        component_sha = _optional_digest(document.get("component_pdf_sha256"), "component PDF")
        package_id = _optional_digest(document.get("package_sha256"), "component package")
        plan_id = _optional_digest(review.get("basic_review_object"), "component review")
        if (
            component_sha is None
            or package_id is None
            or plan_id is None
            or component_sha != segment.get("fingerprint")
            or component_sha != review.get("component_pdf_sha256")
            or any(document.get(key) != segment.get(key) for key in ("source_id", "start_page", "page_count"))
            or any(review.get(key) != segment.get(key) for key in ("source_id", "start_page", "page_count"))
        ):
            raise SyncPlanError("The component reference disagrees with the PDF layout")
        package = verified_json(package_id)
        plan = verified_json(plan_id)
        if (
            package.get("kind") != "thoughts-document-component-package"
            or package.get("source_id") != document["source_id"]
            or package.get("pdf_sha256") != component_sha
            or package.get("page_count") != document["page_count"]
            or package.get("basic_review_object") != plan_id
            or not isinstance(package.get("segment"), dict)
            or package["segment"].get("start_page") != 1
            or plan.get("schema_version") != 1
            or plan.get("pdf_sha256") != component_sha
        ):
            raise SyncPlanError("A component package does not match its verified PDF")
        dependencies.extend(
            (
                SourcePlanDependency(package_id, "document_component_package"),
                SourcePlanDependency(plan_id, "component_review"),
            )
        )
    return dependencies


def reader_source_dependencies(
    manifest: Any,
    publication_filename: str | None = None,
    *,
    include_source_resources: bool = False,
    include_component_packages: bool = False,
    include_markdown_evidence: bool = True,
    read_object: Callable[[str], bytes] | None = None,
) -> list[SourcePlanDependency]:
    """Resolve the exact immutable objects needed by the reader-v1 profile."""
    if not isinstance(manifest, dict):
        raise SyncPlanError("The source artifact is not a JSON object")
    if manifest.get("kind") != "thoughts-document-source-artifact":
        raise SyncPlanError("The source artifact has an invalid kind")
    if publication_filename is not None and manifest.get("filename") != publication_filename:
        raise SyncPlanError("The source artifact belongs to a different publication")
    raw_pdf = manifest.get("pdf")
    raw_segments = raw_pdf.get("segments") if isinstance(raw_pdf, dict) else None
    if isinstance(raw_segments, list) and any(
        isinstance(segment, dict) and segment.get("source_format") == "markdown" for segment in raw_segments
    ):
        if read_object is None:
            raise SyncPlanError("Markdown source artifacts require renderer evidence verification")
        from .markdown_artifacts import validate_markdown_artifact

        try:
            validate_markdown_artifact(manifest, read_object)
        except (KeyError, TypeError, ValueError, RuntimeError, OSError) as error:
            raise SyncPlanError(str(error)) from error

    dependencies: dict[tuple[str, str], SourcePlanDependency] = {}

    def add(value: Any, role: str, field: str) -> None:
        digest = _optional_digest(value, field)
        if digest is None:
            raise SyncPlanError(f"The source artifact is missing {field}")
        dependencies[(digest, role)] = SourcePlanDependency(digest, role)

    sources = manifest.get("sources", [])
    if not isinstance(sources, list):
        raise SyncPlanError("The source artifact has an invalid sources list")
    source_identities: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise SyncPlanError("The source artifact contains an invalid source snapshot")
        path = source.get("path")
        digest = _optional_digest(source.get("sha256"), "source SHA-256")
        object_digest = _optional_digest(source.get("object"), "source object")
        if not _safe_source_path(path) or digest is None or object_digest != digest:
            raise SyncPlanError("The source artifact contains an invalid source snapshot")
        if path in source_identities:
            raise SyncPlanError("The source artifact contains a duplicate source path")
        source_identities[path] = digest
        add(object_digest, "source_snapshot", "source object")

    placement_identities = source_identities
    if "placement_sources" in manifest:
        from .review_metadata import validate_metadata_manifest

        if read_object is None:
            raise SyncPlanError("Source revision mappings require object verification")
        try:
            validate_metadata_manifest(manifest, read_object)
        except (KeyError, TypeError, ValueError) as error:
            raise SyncPlanError("The source artifact has an invalid source revision mapping") from error
        placement_identities = {}
        for source in manifest["placement_sources"]:
            path = source.get("path")
            digest = _optional_digest(source.get("sha256"), "placement source SHA-256")
            if (
                path not in source_identities
                or path in placement_identities
                or digest is None
                or source.get("object") != digest
            ):
                raise SyncPlanError("The source artifact has an invalid placement source")
            placement_identities[path] = digest
            add(digest, "source_snapshot", "placement source object")
        if placement_identities.keys() != source_identities.keys():
            raise SyncPlanError("The source artifact has an incomplete placement source map")

    pdf = manifest.get("pdf")
    if not isinstance(pdf, dict):
        raise SyncPlanError("The source artifact has an invalid PDF description")
    if publication_filename is not None and pdf.get("filename") != publication_filename:
        raise SyncPlanError("The source artifact PDF identity is inconsistent")
    project = pdf.get("project")
    if manifest.get("resources") is None:
        resources = []
    elif not isinstance(project, str):
        raise SyncPlanError("The source artifact has an invalid project")
    else:
        try:
            resources = normalize_manifest_resources(manifest.get("resources"), project)
        except SourceResourceError as error:
            raise SyncPlanError(str(error)) from error
    if include_source_resources:
        for resource in resources:
            add(resource["object"], "source_resource", "source resource object")
    page_count = pdf.get("page_count")
    segments = pdf.get("segments", [])
    if not isinstance(page_count, int) or page_count < 1 or not isinstance(segments, list) or not segments:
        raise SyncPlanError("The source artifact has an invalid segment list")
    expected_start = 1
    for segment in segments:
        if not isinstance(segment, dict):
            raise SyncPlanError("The source artifact contains an invalid PDF segment")
        start = segment.get("start_page")
        count = segment.get("page_count")
        if start != expected_start or not isinstance(count, int) or count < 1:
            raise SyncPlanError("The source artifact has a discontinuous page layout")
        expected_start += count
        if not segment.get("source_available", True):
            continue
        if segment.get("source_format") == "markdown":
            evidence = _optional_digest(segment.get("renderer_evidence_object"), "Markdown evidence")
            if (
                not evidence
                or segment.get("renderer_evidence_sha256") != evidence
                or segment.get("lookup_format") != "thoughts-markdown-source-map-v1"
                or manifest.get("review_placement_model_version") != 4
            ):
                raise SyncPlanError("Markdown segment has an invalid renderer evidence contract")
            if include_markdown_evidence:
                add(evidence, "renderer_evidence", "Markdown evidence object")
        inputs = segment.get("inputs")
        if not isinstance(inputs, list) or not inputs:
            raise SyncPlanError("A source-linked segment has no input map")
        lookup = _optional_digest(segment.get("lookup_object"), "lookup object")
        if lookup is not None:
            if _optional_digest(segment.get("lookup_sha256"), "lookup SHA-256") != lookup:
                raise SyncPlanError("A source-linked segment has an inconsistent lookup identity")
            dependencies[(lookup, "reverse_lookup")] = SourcePlanDependency(
                lookup,
                "reverse_lookup",
            )
        synctex = _optional_digest(segment.get("synctex_object"), "SyncTeX object")
        if lookup is None and synctex is None:
            raise SyncPlanError("A source-linked segment has no usable lookup object")
        # Android's native source navigation needs raw SyncTeX even with a lookup.
        if synctex is not None:
            if _optional_digest(segment.get("synctex_sha256"), "SyncTeX SHA-256") != synctex:
                raise SyncPlanError("A source-linked segment has an inconsistent SyncTeX identity")
            dependencies[(synctex, "raw_synctex_fallback")] = SourcePlanDependency(
                synctex,
                "raw_synctex_fallback",
            )
        for input_record in inputs:
            if not isinstance(input_record, dict):
                raise SyncPlanError("A source-linked segment has an invalid input map")
            source_file = input_record.get("source_file")
            source_sha256 = _optional_digest(input_record.get("source_sha256"), "input source SHA-256")
            if (
                source_file not in source_identities
                or placement_identities[source_file] != source_sha256
                or not isinstance(input_record.get("synctex_path"), str)
                or not input_record["synctex_path"]
            ):
                raise SyncPlanError("A source-linked segment has an inconsistent input map")
    if expected_start - 1 != page_count:
        raise SyncPlanError("The source artifact page count does not match its layout")

    proposals = manifest.get("proposal_objects", [])
    if not isinstance(proposals, list):
        raise SyncPlanError("The source artifact has an invalid proposal-object list")
    proposal_ids: set[str] = set()
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise SyncPlanError("The source artifact contains an invalid proposal identity")
        proposal_id = proposal.get("id")
        if (
            not isinstance(proposal_id, str)
            or not PROPOSAL_ID_PATTERN.fullmatch(proposal_id)
            or proposal_id in proposal_ids
        ):
            raise SyncPlanError("The source artifact contains an invalid proposal identity")
        if _optional_digest(proposal.get("object"), "proposal object") is None:
            raise SyncPlanError("The source artifact is missing a proposal object identity")
        proposal_ids.add(proposal_id)

    inline_proposals = manifest.get("agent_proposals", [])
    if not isinstance(inline_proposals, list):
        raise SyncPlanError("The source artifact has an invalid inline proposal list")
    inline_ids: set[str] = set()
    for proposal in inline_proposals:
        proposal_id = proposal.get("id") if isinstance(proposal, dict) else None
        if not isinstance(proposal_id, str) or proposal_id not in proposal_ids or proposal_id in inline_ids:
            raise SyncPlanError("The source artifact has an inconsistent inline proposal identity")
        inline_ids.add(proposal_id)
    if inline_ids != proposal_ids:
        raise SyncPlanError("The source artifact proposal identities are incomplete")

    if include_component_packages:
        if read_object is None:
            raise SyncPlanError("Component packages require object verification")
        for dependency in component_package_dependencies(manifest, read_object):
            dependencies[(dependency.sha256, dependency.role)] = dependency

    return sorted(dependencies.values(), key=lambda item: (item.role, item.sha256))
