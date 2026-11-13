# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Device-owned immutable download snapshots, independent of later Library edits."""

from collections import OrderedDict
from threading import Lock

from .schemas import SyncPlan, SyncPlanObject


class ReaderPlanError(ValueError):
    pass


class ReaderPlans:
    """Retain bounded transfer snapshots and index their exact authorized objects.

    The client binds activation to its selected publication snapshot. Once transfer
    starts, a concurrent annotation/publication update must not invalidate bytes
    already selected by digest. Object existence and integrity are still checked
    by the download endpoint on every request.
    """

    def __init__(self, capacity: int = 128):
        self._capacity = capacity
        self._plans: OrderedDict[tuple[str, str], dict[str, SyncPlanObject]] = OrderedDict()
        self._lock = Lock()

    def register(self, device_id: str, plan: SyncPlan) -> None:
        key = (device_id, plan.plan_id)
        with self._lock:
            self._plans[key] = {item.sha256: item.model_copy(deep=True) for item in plan.objects}
            self._plans.move_to_end(key)
            while len(self._plans) > self._capacity:
                self._plans.popitem(last=False)

    def object(self, device_id: str, plan_id: str, sha256: str) -> SyncPlanObject:
        with self._lock:
            key = (device_id, plan_id)
            objects = self._plans.get(key)
            if objects is None:
                raise ReaderPlanError("The reader synchronization plan was superseded")
            planned = objects.get(sha256)
            if planned is None:
                raise ReaderPlanError("The object is outside the reader synchronization plan")
            self._plans.move_to_end(key)
            return planned
