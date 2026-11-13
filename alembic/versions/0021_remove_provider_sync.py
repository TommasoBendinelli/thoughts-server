# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Remove Overleaf/Git provider synchronization.

Thoughts Server never contacts Overleaf or a Git remote, so the provider
registry and its job queue are dropped.

Revision ID: 0021_remove_provider_sync
Revises: 0020_document_identity
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0021_remove_provider_sync"
down_revision = "0020_document_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "provider_sync_jobs" in tables:
        op.drop_table("provider_sync_jobs")
    if "provider_projects" in tables:
        op.drop_table("provider_projects")


def downgrade() -> None:
    # Recreate the empty tables exactly as 0019 defined them.
    path = Path(__file__).with_name("0019_provider_sync.py")
    spec = importlib.util.spec_from_file_location("_provider_sync_0019", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()
