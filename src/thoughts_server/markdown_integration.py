# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Markdown adapter for the existing component build and immutable source bundle."""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

from pypdf import PdfReader

from .markdown_renderer import render_markdown
from .markdown_source import MarkdownError, digest, parse_markdown
from .project_documents import ProjectDocument
from .render_types import SourceBuild

MARKDOWN_CAPABILITY = "markdown_source_v1"
MARKDOWN_LOOKUP_FORMAT = "thoughts-markdown-source-map-v1"


def compile_markdown(project: Path, document: ProjectDocument) -> SourceBuild:
    from .thought_ignore import ThoughtIgnore
    ignored = ThoughtIgnore(project)

    def load_dependency(relative):
        path = project / relative
        if (ignored.excludes(relative) or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(project.resolve())
                or any(parent.is_symlink() for parent in path.parents if parent != project and parent.is_relative_to(project))):
            raise MarkdownError("Markdown dependency is unavailable or outside its connected folder: " + relative)
        return path.read_bytes()

    kwargs = {}
    if document.stylesheet:
        kwargs = {"stylesheet_path": document.stylesheet, "stylesheet": load_dependency(document.stylesheet)}
    from .source_owner import folder_owner
    source = project / document.source
    result = render_markdown(
        source.read_bytes(), source_path=document.source, dependency_loader=load_dependency,
        link_folder=folder_owner(project), **kwargs
    )
    build_root = project / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="markdown-", dir=build_root))
    result.write(temporary / "verified")
    output = project / "pdf" / document.output
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(temporary / "verified/document.pdf", output)
    from .source_owner import short_name
    return SourceBuild(
        folder_owner(project),
        short_name(folder_owner(project)),
        document.source,
        document.identity,
        source,
        output,
        None,
        None,
        source_format="markdown",
        renderer_manifest=temporary / "verified/manifest.json",
        title=document.title,
    )


def cache_markdown_build(source: SourceBuild, builds_root: Path) -> None:
    if source.renderer_manifest is None:
        raise MarkdownError("Markdown component has no verified build evidence")
    manifest = json.loads(source.renderer_manifest.read_bytes())
    fingerprint = digest(source.pdf.read_bytes())
    if manifest["pdf_sha256"] != fingerprint:
        raise MarkdownError("Markdown component changed after placement verification")
    cache = builds_root / ("markdown-" + digest(source.source_id.encode())[:16] + "-" + fingerprint)
    if cache.exists():
        shutil.rmtree(cache)
    shutil.copytree(source.renderer_manifest.parent, cache)
    (cache / "mapping.json").write_text(
        json.dumps(
            {
                "source_id": source.source_id,
                "source_fingerprint": fingerprint,
                "source_format": "markdown",
                "project": source.project,
                "title": source.title,
            },
            sort_keys=True,
        )
        + "\n"
    )


def _source_line(raw: bytes, offset: int) -> int:
    return raw[:offset].count(b"\n") + 1


def markdown_lookup(manifest: dict, raw: bytes, source_file: str, pdf_path: Path) -> bytes:
    """Reuse the compact page ZIP transport, with an explicit Markdown discriminator.

    The first eight TSV columns retain the reader's geometric lookup contract;
    additional byte bounds support precise selection without inventing SyncTeX.
    """
    pages = PdfReader(pdf_path).pages
    rows = defaultdict(list)
    for identifier, fragment in manifest["fragments"].items():
        start = fragment["source_ranges"][0]["byte_start"]
        end = fragment["source_ranges"][-1]["byte_end"]
        for location in fragment["locations"]:
            index = location["page_index"]
            page = pages[index]
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            x0, y0, x1, y1 = location["region"]
            rows[index + 1].append(
                f"h\t{source_file}\t{_source_line(raw, start)}\t{x0 * w:.8g}\t{y1 * h:.8g}\t"
                f"{(x1 - x0) * w:.8g}\t{(y1 - y0) * h:.8g}\t0\t{start}\t{end}\t{identifier}\n"
            )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        entries = [
            ("format.json", json.dumps({"schema_version": 1, "format": MARKDOWN_LOOKUP_FORMAT}).encode())
        ]
        entries.extend((f"pages/{page}.tsv", "".join(lines).encode()) for page, lines in sorted(rows.items()))
        for name, content in entries:
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    return output.getvalue()


def markdown_segment(publisher, base: dict) -> dict:
    build = publisher._build(base["source_id"], base["fingerprint"])
    if build is None or build.mapping.get("source_format") != "markdown":
        raise MarkdownError("Exact Markdown component build is unavailable")
    evidence_raw = (build.directory / "manifest.json").read_bytes()
    evidence = json.loads(evidence_raw)
    pdf_path = build.directory / "document.pdf"
    if evidence["pdf_sha256"] != base["fingerprint"] or digest(pdf_path.read_bytes()) != base["fingerprint"]:
        raise MarkdownError("Markdown renderer evidence belongs to another component PDF")
    source_file = build.mapping["project"] + "/" + evidence["source_path"]
    source_sha = evidence["source_sha256"]
    raw = (build.directory / "blobs" / source_sha).read_bytes()
    if digest(raw) != source_sha:
        raise MarkdownError("Markdown source snapshot failed verification")
    from .renderer_tools.publish_phone_source_bundle import parse_agent_proposal_source
    markdown_proposals, proposal_issues = parse_agent_proposal_source(source_file, raw)
    parsed = parse_markdown(raw)
    evidence_object = publisher._store_object(evidence_raw)
    source_object = publisher._store_object(raw)
    comments = {}
    locations = defaultdict(list)
    for key, marker in evidence["markers"].items():
        if not key.startswith("comment:"):
            continue
        _, identifier, _segment = key.split(":")
        metadata = parsed.metadata.get(key, {})
        logical = publisher.logical_comments.get(identifier, {})
        body = logical.get("body") or {}
        contents = metadata.get("contents", body.get("text", "") if isinstance(body, dict) else str(body))
        comment = comments.setdefault(
            identifier,
            {
                "id": identifier,
                "contents": contents,
                "source_file": source_file,
                "source_sha256": source_sha,
                "line": _source_line(raw, marker["byte_start"]),
                "target": {"byte_start": marker["byte_start"], "byte_end": marker["byte_end"]},
                "locations": [],
            },
        )
        comment["target"]["byte_start"] = min(comment["target"]["byte_start"], marker["byte_start"])
        comment["target"]["byte_end"] = max(comment["target"]["byte_end"], marker["byte_end"])
        for location in marker["locations"]:
            locations[identifier].append(
                {
                    "status": "exact",
                    "page": location["page_index"] + 1,
                    "regions": [location["region"]] if "region" in location else [],
                    "point": location.get("point", []),
                    "verification": {
                        "method": location["method"],
                        "resolver_version": 4,
                        "source_format": "markdown",
                        "renderer_evidence_sha256": evidence_object,
                        "fragment_id": location["fragment_id"],
                        "marker_ids": [key],
                        "occurrence": location.get("fragment_index", 1),
                        "candidate_count": 1,
                    },
                }
            )
    for identifier, comment in comments.items():
        target = comment["target"]
        a, b = target["byte_start"], target["byte_end"]
        target.update(
            {
                "sha256": digest(raw[a:b]),
                "preview": raw[a:b].decode()[:2000],
                "line_start": _source_line(raw, a),
                "line_end": _source_line(raw, b),
                "column_start": len(raw[:a].decode().rsplit("\n", 1)[-1]) + 1,
                "column_end": len(raw[:b].decode().rsplit("\n", 1)[-1]) + 1,
            }
        )
        if identifier in publisher.review_comments:
            raise MarkdownError(f"Comment identity occurs in more than one source: {identifier}")
        publisher.review_comments[identifier] = comment
    card_ids, card_fragments = markdown_review_anchors(publisher, evidence, raw, source_file, evidence_object)
    for proposal in markdown_proposals:
        proposal["source_sha256"] = source_sha
        previous = publisher.proposals.get(proposal["id"])
        if previous is not None and any(
            previous.get(key) != proposal.get(key)
            for key in ("source_file", "kind", "comment", "before", "after", "created_at", "conversation_id", "conversation_url")
        ):
            raise MarkdownError(f"Duplicate Markdown proposal ID: {proposal['id']}")
        publisher.proposals[proposal["id"]] = proposal
    record = {
        "path": source_file,
        "sha256": source_sha,
        "object": source_object,
        "bytes": len(raw),
        "source_format": "markdown",
        "proposal_ids": [proposal["id"] for proposal in markdown_proposals],
        "review_comment_ids": sorted(comments),
        "review_card_anchor_ids": card_ids,
        # Optional, present only when a proposal block in this source is broken.
        **({"proposal_issues": proposal_issues} if proposal_issues else {}),
    }
    if source_file in publisher.source_objects and publisher.source_objects[source_file] != record:
        raise MarkdownError("Markdown source has conflicting snapshots")
    publisher.source_objects[source_file] = record
    lookup = publisher._store_object(markdown_lookup(evidence, raw, source_file, pdf_path))
    component_object = publisher._store_object(pdf_path.read_bytes())
    dependencies = [component_object]
    for dependency in evidence["dependencies"]:
        content = (build.directory / "blobs" / dependency["sha256"]).read_bytes()
        if digest(content) != dependency["sha256"]:
            raise MarkdownError("Markdown renderer dependency failed verification")
        dependencies.append(publisher._store_object(content))
    return {
        **base,
        "size_bytes": pdf_path.stat().st_size,
        "title": build.mapping.get("title"),
        "source_format": "markdown",
        "source_available": True,
        "primary_source_file": source_file,
        "primary_source_sha256": source_sha,
        "lookup_format": MARKDOWN_LOOKUP_FORMAT,
        "lookup_object": lookup,
        "lookup_sha256": lookup,
        "component_pdf_object": component_object,
        "renderer_evidence_object": evidence_object,
        "renderer_evidence_sha256": evidence_object,
        "renderer_dependency_objects": sorted(set(dependencies)),
        "inputs": [{"synctex_path": source_file, "source_file": source_file, "source_sha256": source_sha}],
        "_review_comment_locations": dict(locations),
        "_review_card_fragments": card_fragments,
    }


def markdown_review_anchors(publisher, evidence, raw, source_file, evidence_object):
    """Publish card markers through the same verified Markdown renderer evidence."""
    identifiers, fragments = [], {}
    for key, marker in evidence["markers"].items():
        if not key.startswith("review:"):
            continue
        _, card_id, role, segment_id = key.split(":")
        if role != "answer":
            continue
        identity = (card_id, segment_id)
        if identity in publisher.review_card_anchors:
            raise MarkdownError("Review card marker occurs in more than one source")
        a, b = marker["byte_start"], marker["byte_end"]
        publisher.review_card_anchors[identity] = {
            "card_id": card_id, "segment_id": segment_id, "source_file": source_file,
            "source_format": "markdown", "source_sha256": evidence["source_sha256"],
            "content_sha256": digest(raw[a:b]), "line_start": _source_line(raw, a),
            "line_end": _source_line(raw, b), "placement": {"status": "unresolved", "regions": []},
        }
        identifiers.append(f"{card_id}:{segment_id}")
        fragments[identity] = [{
            "page": loc["page_index"] + 1,
            **dict(zip(("left", "top", "right", "bottom"), loc["region"], strict=True)),
            "resolver_version": 4, "verification_method": loc["method"],
            "renderer_evidence_sha256": evidence_object, "marker_ids": [key],
            "fragment_id": loc["fragment_id"],
        } for loc in marker["locations"] if "region" in loc]
    return sorted(identifiers), fragments
