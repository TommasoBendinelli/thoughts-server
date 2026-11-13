# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Validate Markdown evidence when packaging, ingesting, and planning a transfer."""

from __future__ import annotations

import json
import re

from .markdown_source import MarkdownError, digest


def validate_markdown_artifact(manifest, read_object):
    pdf = manifest.get("pdf", {})
    segments = [s for s in pdf.get("segments", []) if s.get("source_format") == "markdown"]
    if not segments:
        if manifest.get("review_placement_model_version") == 4 or any(
            location.get("proof", {}).get("resolver_version") == 4
            for record in manifest.get("review_correspondences", [])
            for location in record.get("locations", [])
        ):
            raise MarkdownError("Markdown placement has no renderer evidence segment")
        return
    if manifest.get("review_placement_model_version") != 4:
        raise MarkdownError("Markdown source artifact requires placement model 4")

    def verified(identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise MarkdownError("Missing Markdown evidence identity")
        raw = read_object(identity)
        if digest(raw) != identity:
            raise MarkdownError("Markdown artifact object failed digest verification")
        return raw

    evidence_by_object = {}
    sources = {source["path"]: source for source in manifest["sources"]}
    for segment in segments:
        identity = segment.get("renderer_evidence_object")
        if identity != segment.get("renderer_evidence_sha256"):
            raise MarkdownError("Markdown evidence pointer has inconsistent hashes")
        evidence = json.loads(verified(identity))
        source_file = segment.get("primary_source_file")
        source = sources.get(source_file, {})
        if (
            evidence.get("kind") != "markdown_renderer_evidence"
            or evidence.get("schema_version") != 1
            or evidence.get("pdf_sha256") != segment.get("fingerprint")
            or evidence.get("source_sha256") != source.get("sha256")
            or evidence.get("source_sha256") != segment.get("primary_source_sha256")
            or source_file != pdf.get("project", "") + "/" + evidence.get("source_path", "")
            or evidence.get("validation", {}).get("pages") != segment.get("page_count")
            or segment.get("lookup_format") != "thoughts-markdown-source-map-v1"
        ):
            raise MarkdownError("Markdown renderer evidence does not match the published source segment")
        if source.get("object") != source.get("sha256"):
            raise MarkdownError("Markdown source object identity is inconsistent")
        raw = verified(source["object"])
        if segment.get("component_pdf_object") != evidence["pdf_sha256"]:
            raise MarkdownError("Markdown component identity is inconsistent")
        required = {dependency["sha256"] for dependency in evidence["dependencies"]} | {
            evidence["pdf_sha256"]
        }
        if set(segment.get("renderer_dependency_objects", [])) != required:
            raise MarkdownError("Markdown renderer dependency snapshot is incomplete")
        for fragment in evidence["fragments"].values():
            for span in fragment["source_ranges"]:
                start, end = span["byte_start"], span["byte_end"]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(raw)
                    or digest(raw[start:end]) != span["sha256"]
                ):
                    raise MarkdownError("Markdown fragment is not bound to its original source bytes")
        evidence_by_object[identity] = (segment, evidence)
    for anchor in manifest.get("review_card_anchors", []):
        if anchor.get("source_format") != "markdown":
            continue
        key = f"review:{anchor.get('card_id')}:answer:{anchor.get('segment_id')}"
        expected = []
        for identity, (segment, evidence) in evidence_by_object.items():
            marker = evidence["markers"].get(key)
            if marker is None:
                continue
            raw = verified(evidence["source_sha256"])
            if (anchor.get("source_file") != segment["primary_source_file"]
                    or anchor.get("source_sha256") != evidence["source_sha256"]
                    or anchor.get("content_sha256") != digest(
                        raw[marker["byte_start"]:marker["byte_end"]])):
                raise MarkdownError("Review card differs from its original Markdown source")
            for location in marker["locations"]:
                if "region" in location:
                    expected.append((identity, location["fragment_id"],
                        location["page_index"] + segment["start_page"],
                        location["region"], location["method"]))
        actual = anchor.get("placement", {}).get("regions", [])
        if not expected or len(actual) != len(expected):
            raise MarkdownError("Review card has incomplete Markdown placement")
        for region in actual:
            proof = (region.get("renderer_evidence_sha256"), region.get("fragment_id"),
                region.get("page"), [region.get(k) for k in ("left", "top", "right", "bottom")],
                region.get("verification_method"))
            if (proof not in expected or region.get("marker_ids") != [key]
                    or region.get("resolver_version") != 4
                    or region.get("pdf_filename") != pdf.get("filename")):
                raise MarkdownError("Review card differs from its verified Markdown placement")
            expected.remove(proof)
    for correspondence in manifest.get("review_correspondences", []):
        for target in correspondence.get("source_targets", []):
            source = sources.get(target.get("source_file"), {})
            if source.get("source_format") != "markdown":
                continue
            raw = verified(source["object"])
            start, end = target.get("byte_start"), target.get("byte_end")
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end <= len(raw)
                or target.get("source_sha256") != source["sha256"]
                or target.get("text_sha256") != digest(raw[start:end])
            ):
                raise MarkdownError("Markdown source target does not match its original bytes")
        for location in correspondence.get("locations", []):
            proof = location.get("proof", {})
            if proof.get("resolver_version") != 4:
                continue
            pair = evidence_by_object.get(proof.get("renderer_evidence_sha256"))
            if pair is None:
                raise MarkdownError("Markdown placement has no renderer evidence in this artifact")
            segment, evidence = pair
            markers = proof.get("marker_ids", [])
            marker = evidence["markers"].get(markers[0]) if len(markers) == 1 else None
            if marker is None or proof.get("source_sha256") != evidence["source_sha256"]:
                raise MarkdownError("Markdown placement has no matching source marker")
            expected = [
                item
                for item in marker["locations"]
                if item["fragment_id"] == proof.get("fragment_id")
                and item.get("fragment_index", 1) == proof.get("occurrence")
                and item["page_index"] + segment["start_page"] - 1 == location.get("page_index")
            ]
            if (
                len(expected) != 1
                or expected[0]["method"] != proof.get("method")
                or location.get("build_sha256") != segment["fingerprint"]
                or location.get("pdf_sha256") != pdf["pdf_sha256"]
                or location.get("regions", []) != ([expected[0]["region"]] if "region" in expected[0] else [])
                or location.get("point", []) != expected[0].get("point", [])
            ):
                raise MarkdownError("Markdown placement differs from its verified renderer observation")


def validate_markdown_publication(pdf_path, segments, read_object):
    """Check aggregation preserved the exact painted geometry of each component."""
    import io

    import pdfplumber

    markdown = [s for s in segments if s.get("source_format") == "markdown"]
    if not markdown:
        return
    with pdfplumber.open(pdf_path) as final:
        for segment in markdown:
            raw = read_object(segment["component_pdf_object"])
            if digest(raw) != segment["fingerprint"]:
                raise MarkdownError("Markdown component PDF failed verification")
            with pdfplumber.open(io.BytesIO(raw)) as component:
                if len(component.pages) != segment["page_count"]:
                    raise MarkdownError("Markdown component pagination changed")
                for index, expected in enumerate(component.pages):
                    page = final.pages[segment["start_page"] - 1 + index]
                    if (page.width, page.height) != (expected.width, expected.height):
                        raise MarkdownError("Publication changed Markdown page dimensions")
                    for kind in ("chars", "curves", "rects", "lines", "images"):
                        left, right = getattr(page, kind), getattr(expected, kind)
                        if len(left) != len(right):
                            raise MarkdownError(f"Publication changed Markdown {kind} count")
                        for a, b in zip(left, right, strict=True):
                            if a.get("text") != b.get("text") or any(
                                abs(a[key] - b[key]) > 0.01 for key in ("x0", "top", "x1", "bottom")
                            ):
                                raise MarkdownError(f"Publication changed Markdown {kind} placement")
