# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import pytest

from thoughts_server.reader_plans import ReaderPlanError, ReaderPlans
from thoughts_server.schemas import SyncPlan, SyncPlanObject


def plan(identity="a", object_id="b"):
    return SyncPlan(
        include_voice_media=False, plan_id=identity * 64, latest_server_sequence=1,
        library_revision="c" * 64, metadata_download_bytes=0, download_bytes=5,
        objects=[SyncPlanObject(
            sha256=object_id * 64, size_bytes=5, kind="source", roles=["source_snapshot"],
        )],
    )


def test_same_plan_for_two_devices_does_not_revoke_first_transfer():
    registry = ReaderPlans()
    snapshot = plan()
    registry.register("first", snapshot)
    registry.register("second", snapshot)
    snapshot.objects.clear()
    for device in ("first", "second"):
        assert registry.object(device, "a" * 64, "b" * 64).size_bytes == 5
    with pytest.raises(ReaderPlanError):
        registry.object("third", "a" * 64, "b" * 64)


def test_eviction_preserves_recently_active_transfer_and_rejects_unplanned_object():
    registry = ReaderPlans(capacity=2)
    registry.register("device", plan("a"))
    registry.register("device", plan("b"))
    with pytest.raises(ReaderPlanError):
        registry.object("device", "a" * 64, "d" * 64)
    registry.object("device", "a" * 64, "b" * 64)
    registry.register("device", plan("c"))
    assert registry.object("device", "a" * 64, "b" * 64)
    with pytest.raises(ReaderPlanError):
        registry.object("device", "b" * 64, "b" * 64)
