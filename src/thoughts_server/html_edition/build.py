# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Build one document's HTML edition from a materialized source tree."""
from __future__ import annotations

import json
from pathlib import Path

from . import CONVERTER_VERSION, ENTRY, MANIFEST_KIND, SCHEMA_VERSION, anchor_items
from .bundle import Edition, build_edition


def find_document(project: Path, filename: str):
    """The selected source document of ``project`` whose PDF is ``filename``, or None."""
    from ..project_documents import project_documents

    for document in project_documents(project, allow_missing_inputs=True) or []:
        if document.output == filename and document.source_format in {"latex", "markdown"}:
            return document
    return None


def convert_document(docs_root: Path, project: Path, document, work: Path, *, bibliography: Path | None = None,
                     sandbox: bool = False, timeout: int = 120, node: str = "node",
                     font_cache: Path | None = None) -> Edition:
    """Raises latex.ConversionFailed, MarkdownError, bundle.EditionInvalid or RuntimeError on failure."""
    if document.source_format == "markdown":
        from .markdown import convert

        raw = convert(docs_root, project, document)
    elif document.source_format == "latex":
        from .latex import convert

        raw = convert(docs_root, project, document, work, bibliography=bibliography, sandbox=sandbox,
                      timeout=timeout, font_cache=font_cache)
    else:
        raise ValueError(f"no HTML edition for {document.source_format} documents")
    return build_edition(raw, node=node)


def manifest(edition: Edition, file_hashes: dict[str, str], *, document_id: str, pdf_sha256: str,
             source_snapshot_sha256: str | None) -> dict:
    return {
        "kind": MANIFEST_KIND,
        "schema": SCHEMA_VERSION,
        "document_id": document_id,
        "source_format": edition.source_format,
        "source_snapshot_sha256": source_snapshot_sha256,
        "pdf_sha256": pdf_sha256,
        "converter": {**edition.converter, "edition": CONVERTER_VERSION},
        "entry": ENTRY,
        "files": dict(sorted(file_hashes.items())),
        "anchors": edition.anchors,
        # Per card / comment: its marker keys in this edition (cards also their question segments).
        "items": anchor_items(edition.anchors),
        "source_map": edition.source_map,
        # SHA-256 of the source files that data-src positions refer to (for source_range).
        "source_files": dict(sorted(edition.source_files.items())),
        "warnings": edition.warnings,
    }


def manifest_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
