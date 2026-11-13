# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add content-tree build-input snapshot metadata.

Revision ID: 0015_input_snapshot_v2
Revises: 0014_doc_artifacts
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_input_snapshot_v2"
down_revision = "0014_doc_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("build_input_snapshots")}
    if "schema_version" not in columns:
        op.add_column(
            "build_input_snapshots",
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        )
    if "project" not in columns:
        op.add_column("build_input_snapshots", sa.Column("project", sa.String(length=255)))
    if "discovery_policy" not in columns:
        op.add_column(
            "build_input_snapshots",
            sa.Column("discovery_policy", sa.String(length=64)),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("build_input_snapshots")}
    if "discovery_policy" in columns:
        op.drop_column("build_input_snapshots", "discovery_policy")
    if "project" in columns:
        op.drop_column("build_input_snapshots", "project")
    if "schema_version" in columns:
        op.drop_column("build_input_snapshots", "schema_version")
