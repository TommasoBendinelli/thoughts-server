# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The auto-deleted comment report lists past automatic deletions and never writes."""

from __future__ import annotations

import json
import sys
import uuid

import pytest
from sqlalchemy import select

from thoughts_server.auto_deleted_comments import COLUMNS, auto_deleted_comment_report
from thoughts_server.database import Database
from thoughts_server.models import DocumentIdentity, Entity

ALPHA = str(uuid.uuid4())


def _logical(identifier, *, deleted=True, parent=None, origin=None, **payload):
    return Entity(entity_type="logical_comment", collection="comments", entity_id=identifier, revision=2,
                  deleted=deleted, winner_operation_id=str(uuid.uuid4()), payload={
                      "comment_id": identifier, "thread_root_id": parent or identifier, "parent_id": parent,
                      "deleted": deleted, "contents": f"Comment {identifier}", "updated_at": "2026-08-01T00:00:00Z",
                      "origin": origin or {"collection": "beta"}, **payload})


def _placement(identifier, collection, *, deleted=True, **payload):
    return Entity(entity_type="annotation", collection=collection, entity_id=identifier, revision=3,
                  deleted=deleted, winner_operation_id=str(uuid.uuid4()),
                  payload={"id": identifier, "annotation_id": identifier, "deleted": deleted, **payload})


@pytest.fixture
def seeded(settings, tmp_path):
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        session.add(DocumentIdentity(document_id=ALPHA, filename="alpha.pdf",
                                     source_path="docs-alpha/tex/source/main.tex"))
        session.add_all([
            # Server marker removal, with a cascaded reply.
            _logical("server", origin={"collection": "alpha", "document_id": ALPHA},
                     contents="  Server\n removed   text ", deletion_reason="canonical_source_marker_removed",
                     deleted_at="2026-09-01T00:00:00Z",
                     scope={"state": "source_linked", "anchor_id": "server",
                            "source_file": "docs-alpha/tex/source/main.tex", "source_line": 7},
                     source_deletion={"operation_id": "op-1", "sources": [{"path": "docs-alpha/tex/source/main.tex"}]}),
            _logical("server-reply", parent="server"),
            # A desktop sidecar tombstone that reached the logical comment.
            _logical("desktop", updated_at="2026-09-02T00:00:00Z", scope={"state": "pdf_only"}),
            _placement("desktop", "beta", source_anchor_deletion=True, tex_file="docs-beta/tex/source/ch.tex",
                       tex_line=3),
            # A legacy placement without a logical comment.
            _placement("legacy", "gamma", source_anchor_deletion=True, deletion_reason="source_anchor_removed",
                       deleted_at="2026-09-03T00:00:00Z", contents="Legacy text", tex_file="docs-gamma/main.tex"),
            # Exclusions.
            _logical("explicit"),
            _placement("explicit", "beta"),
            _logical("user", deletion_reason="user_deleted"),
            _placement("user", "beta", source_anchor_deletion=True),
            _logical("retired", deletion_reason="document_deleted"),
            _logical("live", deleted=False),
            _placement("live", "beta", deleted=False, source_anchor_deletion=True),
        ])
        session.commit()
    docs = tmp_path / "docs"
    (docs / "comments").mkdir(parents=True)
    (docs / "comments" / "review_resolutions.jsonl").write_text("\n".join(json.dumps(record) for record in [
        {"id": "registry", "deleted": True, "source_anchor_deletion": True, "deleted_at": "2026-09-04T00:00:00Z",
         "deletion_reason": "source_anchor_removed", "revision": 1},
        {"id": "legacy", "deleted": True, "source_anchor_deletion": True, "deleted_at": "2026-09-03T00:00:00Z",
         "revision": 1},
    ]) + "\n")
    (docs / "comments" / "delta_comments.jsonl").write_text("\n".join(json.dumps(record) for record in [
        {"id": "sidecar", "deleted": True, "source_anchor_deletion": True, "deleted_at": "2026-09-05T00:00:00Z",
         "tex_file": "docs-delta/tex/source/main.tex", "tex_line": 12, "contents": "Sidecar text", "revision": 2},
        {"id": "kept", "deleted": False, "contents": "Live", "revision": 1},
        {"id": "removed", "deleted": True, "contents": "Explicit", "revision": 2},
    ]) + "\n")
    return database, docs


def _snapshot(database, docs):
    with database.session_factory() as session:
        rows = sorted((e.entity_type, e.collection, e.entity_id, e.revision, e.deleted, json.dumps(e.payload))
                      for e in session.scalars(select(Entity)))
    files = {path.name: path.read_bytes() for path in sorted((docs / "comments").iterdir())}
    return rows, files


def test_report_lists_each_kind_and_excludes_explicit_or_live_comments(seeded):
    database, docs = seeded
    before = _snapshot(database, docs)
    with database.session_factory() as session:
        without_docs = auto_deleted_comment_report(session)
        rows = auto_deleted_comment_report(session, docs)
    assert _snapshot(database, docs) == before
    assert [row["comment_id"] for row in without_docs] == ["server", "desktop", "legacy"]
    assert [(row["comment_id"], row["kind"]) for row in rows] == [
        ("server", "server_marker_removed"),
        ("desktop", "desktop_source_anchor_removed"),
        ("legacy", "legacy_placement"),
        ("registry", "desktop_registry"),
        ("sidecar", "desktop_registry"),
    ]
    server, desktop, legacy, registry, sidecar = rows
    assert server == {
        "comment_id": "server", "kind": "server_marker_removed",
        "deletion_reason": "canonical_source_marker_removed", "document": "alpha.pdf", "document_id": ALPHA,
        "source_file": "docs-alpha/tex/source/main.tex", "source_line": 7, "deleted_at": "2026-09-01T00:00:00Z",
        "replies": 1, "operation_id": "op-1", "excerpt": "Server removed text",
    }
    assert desktop["document"] == "beta.pdf" and desktop["deleted_at"] == "2026-09-02T00:00:00Z"
    assert (desktop["source_file"], desktop["source_line"]) == ("docs-beta/tex/source/ch.tex", 3)
    assert desktop["deletion_reason"] is None
    assert (legacy["document"], legacy["excerpt"], legacy["source_file"]) == ("gamma.pdf", "Legacy text",
                                                                           "docs-gamma/main.tex")
    assert registry["document"] == "" and registry["deletion_reason"] == "source_anchor_removed"
    assert (sidecar["document"], sidecar["source_line"], sidecar["excerpt"]) == ("delta.pdf", 12, "Sidecar text")


@pytest.mark.parametrize("output_format", ["json", "tsv"])
def test_admin_command_prints_report_without_writing(seeded, settings, tmp_path, monkeypatch, capsys,
                                                     output_format):
    from thoughts_server.cli import admin_main

    database, docs = seeded
    before = _snapshot(database, docs)
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "unused-state"))
    monkeypatch.setenv("THOUGHTS_OBJECT_ROOT", str(tmp_path / "unused-objects"))
    monkeypatch.setattr(sys, "argv", ["thoughts-admin", "--database-url", settings.database_url,
                                      "report-auto-deleted-comments", "--format", output_format,
                                      "--docs-root", str(docs)])
    admin_main()
    output = capsys.readouterr().out
    assert _snapshot(database, docs) == before
    assert not (tmp_path / "unused-state").exists() and not (tmp_path / "unused-objects").exists()
    if output_format == "json":
        rows = json.loads(output)
        assert [row["comment_id"] for row in rows] == ["server", "desktop", "legacy", "registry", "sidecar"]
    else:
        lines = output.rstrip("\n").split("\n")
        assert lines[0].split("\t") == list(COLUMNS)
        assert [line.split("\t")[0] for line in lines[1:]] == ["server", "desktop", "legacy", "registry", "sidecar"]
        assert all(len(line.split("\t")) == len(COLUMNS) for line in lines)
        assert lines[1].split("\t")[COLUMNS.index("excerpt")] == "Server removed text"


def test_admin_help_states_the_limitation(monkeypatch, capsys):
    from thoughts_server.cli import admin_main

    monkeypatch.setattr(sys, "argv", ["thoughts-admin", "report-auto-deleted-comments", "--help"])
    with pytest.raises(SystemExit):
        admin_main()
    assert "cannot be told apart from explicit deletes" in " ".join(capsys.readouterr().out.split())
