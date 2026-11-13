# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import json

from thoughts_server.migration import export_workspace
from thoughts_server.models import Entity
from thoughts_server.renderer_tools.lib.pdf_comments import partition_comments_for_pdf


def test_export_respects_canonical_deletion_even_if_placement_survives(client, tmp_path):
    with client.app.state.database.session_factory() as session:
        session.add(Entity(entity_type="annotation", collection="concepts", entity_id="old-comment",
            revision=1, deleted=False, winner_operation_id="old-placement", payload={"id": "old-comment", "contents": "old", "rect": []}))
        session.add(Entity(entity_type="logical_comment", collection="comments", entity_id="old-comment",
            revision=2, deleted=True, winner_operation_id="deleted-comment", payload={"comment_id": "old-comment", "deleted": True}))
        session.commit()
        export_workspace(session, tmp_path / "export", write_manifest=False)
    record = json.loads((tmp_path / "export/comments/concepts_comments.jsonl").read_text())
    assert record["deleted"] is True
    assert record["contents"] == "old"


def test_invalid_comment_does_not_prevent_healthy_annotation_publication():
    good = {"id": "good", "subtype": "Text", "page": 1, "rect": [10, 10, 20, 20]}
    bad = {**good, "id": "bad", "rect": [10, 10]}
    deleted = {**good, "id": "deleted", "deleted": True}
    writeable, skipped = partition_comments_for_pdf([bad, good, deleted], 1)
    assert [x["id"] for x in writeable] == ["good"]
    assert {x["id"] for x in skipped} == {"bad", "deleted"}
    assert bad["rect"] == [10, 10]
