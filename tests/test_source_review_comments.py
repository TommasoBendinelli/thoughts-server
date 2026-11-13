# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import json

import pytest

from thoughts_server.database import Database
from thoughts_server.library import published_source_index_bytes
from thoughts_server.models import Entity, Publication, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.source_review_comments import (
    parse_review_comments,
    refresh_document_review_comments,
)


def test_parse_review_comments_decodes_mirrors_and_keeps_nested_markers():
    source = (
        b"% \\CommentAnchor{ignored}{ignored}\n"
        b"\\CommentAnchor{outer}{Outer \\% note}{"
        b"\\CommentAnchor{inner}{Inner \\textbackslash{} note}{body}}\n"
    )

    comments = parse_review_comments("docs-one/tex/source/main.tex", source)

    assert comments == [
        {
            "id": "outer",
            "contents": "Outer % note",
            "source_file": "docs-one/tex/source/main.tex",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "line": 2,
        },
        {
            "id": "inner",
            "contents": "Inner \\ note",
            "source_file": "docs-one/tex/source/main.tex",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "line": 2,
        },
    ]


def test_parse_review_comments_excludes_duplicate_or_malformed_ids():
    source = (
        b"\\CommentAnchor{duplicate}{first}\n"
        b"\\CommentAnchor{duplicate}{second}\n"
        b"\\CommentAnchor{missing-mirror}\n"
    )

    assert parse_review_comments("docs-one/tex/source/main.tex", source) == []


def test_refresh_projects_current_comments_without_replacing_the_pdf(settings):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    filename = "manual.pdf"
    source_path = "docs-manual/tex/source/main.tex"
    source_raw = b"\\CommentAnchor{comment-one}{Current source comment}{body}\n"
    pdf_raw = b"%PDF-current"
    with database.session_factory() as session:
        source = store.put_bytes(session, source_raw, "text/x-tex")
        pdf = store.put_bytes(session, pdf_raw, "application/pdf")
        session.add(
            SourceFile(
                path=source_path,
                revision=7,
                sha256=source.sha256,
                object_sha256=source.sha256,
                operation_id="source-operation",
                deleted=False,
            )
        )
        previous_source_sha = hashlib.sha256(b"previous source").hexdigest()
        manifest = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": filename,
            "pdf": {
                "filename": filename,
                "pdf_sha256": pdf.sha256,
                "segments": [{
                    "inputs": [{
                        "source_file": source_path,
                        "source_sha256": previous_source_sha,
                    }],
                }],
            },
            "sources": [{
                "path": source_path,
                "sha256": previous_source_sha,
                "object": source.sha256,
                "bytes": len(source_raw),
            }],
            "agent_proposals": [],
            "proposal_objects": [],
            "logical_comments_schema_version": 1,
            "logical_comments": [{
                "schema_version": 1,
                "comment_id": "pdf-only",
                "thread_root_id": "pdf-only",
                "scope": {"state": "pdf_only"},
                "locations": [{
                    "pdf_filename": filename,
                    "pdf_sha256": pdf.sha256,
                    "page": 6,
                    "status": "page_only",
                }],
            }],
        }
        manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        artifact = store.put_bytes(session, manifest_raw, "application/vnd.thoughts.document-source+json")
        session.add(
            Publication(
                filename=filename,
                document_id="manual",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                source_artifact_sha256=artifact.sha256,
                metadata_json={"kind": "source_linked"},
            )
        )
        session.add(
            Entity(
                entity_type="logical_comment",
                collection="comments",
                entity_id="pdf-only",
                revision=1,
                deleted=False,
                winner_operation_id="logical-operation",
                payload={
                    "schema_version": 1,
                    "comment_id": "pdf-only",
                    "thread_root_id": "pdf-only",
                    "contents": "Still only in this PDF",
                    "origin": {"collection": "manual"},
                    "scope": {"state": "pdf_only", "reason": "missing_marker"},
                },
            )
        )
        session.add(
            Entity(
                entity_type="logical_comment",
                collection="comments",
                entity_id="legacy-pdf-id",
                revision=1,
                deleted=False,
                winner_operation_id="logical-source-operation",
                payload={
                    "schema_version": 1,
                    "comment_id": "legacy-pdf-id",
                    "thread_root_id": "legacy-pdf-id",
                    "contents": "Current source comment",
                    "origin": {"collection": "manual"},
                    "scope": {
                        "state": "source_linked",
                        "anchor_id": "comment-one",
                    },
                },
            )
        )
        session.commit()

        bundle_root = settings.state_root / "source-bundle"
        (bundle_root / "objects").mkdir(parents=True)
        (bundle_root / "documents").mkdir(parents=True)
        (bundle_root / "objects" / artifact.sha256).write_bytes(manifest_raw)

        refreshed = refresh_document_review_comments(settings, session, store, {source_path})
        session.commit()

        publication = session.get(Publication, filename)
        assert publication is not None
        assert publication.object_sha256 == hashlib.sha256(pdf_raw).hexdigest()
        assert publication.source_artifact_sha256 == refreshed[filename]
        projected = json.loads(store.path(publication.source_artifact_sha256).read_text())
        assert projected["pdf"]["pdf_sha256"] == manifest["pdf"]["pdf_sha256"]
        assert projected["pdf"]["segments"][0]["inputs"][0]["source_sha256"] == source.sha256
        assert projected["review_comments"] == [
            {
                "id": "comment-one",
                "contents": "Current source comment",
                "source_file": source_path,
                "source_sha256": source.sha256,
                "line": 1,
            }
        ]
        # Logical comments are canonical records; source and PDF placement are
        # derived by builds, not projected by a comment refresh (1e8d215).
        assert projected["logical_comments"] == [
            {
                "schema_version": 1,
                "comment_id": "legacy-pdf-id",
                "thread_root_id": "legacy-pdf-id",
                "contents": "Current source comment",
                "origin": {"collection": "manual"},
                "scope": {
                    "state": "source_linked",
                    "anchor_id": "comment-one",
                },
                "deleted": False,
            },
            {
                "schema_version": 1,
                "comment_id": "pdf-only",
                "thread_root_id": "pdf-only",
                "contents": "Still only in this PDF",
                "origin": {"collection": "manual"},
                "scope": {"state": "pdf_only", "reason": "missing_marker"},
                "deleted": False,
            },
        ]
        source_index_sha256 = hashlib.sha256(published_source_index_bytes(session)).hexdigest()
        assert store.path(source_index_sha256).is_file()


@pytest.mark.parametrize("command", ["CommentAnchor", "ReviewAnchor"])
def test_comment_anchor_names_preserve_identity_and_source_offsets(command):
    from thoughts_server.renderer_tools.publish_phone_source_bundle import (
        parse_review_comments as parse_bundle_comments,
    )
    source = ("Preface é.\n\\" + command + "{same-id}{Same comment}{Exact body}\n").encode()
    parsed = parse_review_comments("note.tex", source)
    bundled = parse_bundle_comments("note.tex", source)
    assert [(item["id"], item["contents"], item["line"]) for item in parsed] == [
        ("same-id", "Same comment", 2)
    ]
    assert [(item["id"], item["contents"], item["line"]) for item in bundled] == [
        ("same-id", "Same comment", 2)
    ]
    target = bundled[0]["target"]
    assert source[target["byte_start"]:target["byte_end"]] == b"Exact body"
    assert source[bundled[0]["macro_start"]:bundled[0]["macro_end"]].startswith(
        ("\\" + command + "{same-id}").encode()
    )


def test_mixed_comment_anchor_names_do_not_hide_duplicate_identifiers():
    source = b"\\CommentAnchor{same}{new}{}\n\\ReviewAnchor{same}{old}{}\n"
    assert parse_review_comments("note.tex", source) == []
