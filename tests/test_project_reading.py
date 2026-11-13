# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import copy

import pytest

from thoughts_server.project_reading import ProjectReading, ReadingDocument


def reading():
    return ProjectReading("project", "Reading", (
        ReadingDocument("first", "First", "a" * 64, 100, 3, "b" * 64),
        ReadingDocument("second", "Second", "c" * 64, 200, 2, "d" * 64),
    ))


def test_reading_roundtrip_and_boundaries():
    project = ProjectReading.parse(reading().payload())
    assert project.page_count == 5
    assert project.pdf_size_bytes == 300
    assert project.locate(2) == (project.documents[0], 2)
    assert project.locate(3) == (project.documents[1], 0)
    assert project.project_page("second", 1) == 4
    for page in (-1, 5, True):
        with pytest.raises(ValueError):
            project.locate(page)
    with pytest.raises(ValueError):
        project.project_page("missing", 0)


def test_reorder_preserves_document_anchors_but_changes_revision():
    before = reading()
    after = ProjectReading(before.project_id, before.title, tuple(reversed(before.documents)))
    assert before.revision != after.revision
    assert before.project_page("first", 1) == 1
    assert after.project_page("first", 1) == 3
    assert after.locate(3) == (before.documents[0], 1)


@pytest.mark.parametrize("change", [
    lambda p: p.update(page_count=4),
    lambda p: p["documents"][0].update(pdf_sha256="../../data"),
    lambda p: p["documents"][0].update(page_count=True),
    lambda p: p["documents"][1].update(document_id="first"),
    lambda p: p["documents"][1].update(pdf_sha256="a" * 64),
    lambda p: p.update(schema_version=2),
])
def test_invalid_or_inconsistent_composition_fails_closed(change):
    payload = copy.deepcopy(reading().payload())
    change(payload)
    with pytest.raises(ValueError):
        ProjectReading.parse(payload)


def test_annotation_package_revision_invalidates_snapshot_without_pdf_change():
    before = reading()
    payload = before.payload()
    payload["documents"][0]["package_sha256"] = "e" * 64
    after = ProjectReading.parse(payload)
    assert before.revision != after.revision
    assert before.documents[0].pdf_sha256 == after.documents[0].pdf_sha256
