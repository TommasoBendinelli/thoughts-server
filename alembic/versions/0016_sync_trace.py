# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add cross-component synchronization trace identities.

Revision ID: 0016_sync_trace
Revises: 0015_input_snapshot_v2
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_sync_trace"
down_revision = "0015_input_snapshot_v2"
branch_labels = None
depends_on = None


TABLES = ("operations", "source_publications", "build_jobs", "publications")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "trace_id" not in columns:
            op.add_column(table, sa.Column("trace_id", sa.String(length=36), nullable=True))
        index_name = f"ix_{table}_trace_id"
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        if index_name not in indexes:
            op.create_index(index_name, table, ["trace_id"])


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_index(f"ix_{table}_trace_id", table_name=table)
        op.drop_column(table, "trace_id")
