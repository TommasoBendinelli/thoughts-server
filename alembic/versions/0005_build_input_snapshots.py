# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add exact build-input and publication provenance identities."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_build_input_snapshots"
down_revision = "0004_source_publication_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "build_input_snapshots" not in tables:
        op.create_table(
            "build_input_snapshots",
            sa.Column("sha256", sa.String(length=64), primary_key=True),
            sa.Column("target", sa.String(length=255), nullable=False),
            sa.Column("manifest_object_sha256", sa.String(length=64), nullable=False),
            sa.Column("entries_json", sa.JSON(), nullable=False),
            sa.Column("created_by", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["manifest_object_sha256"], ["objects.sha256"]),
            sa.ForeignKeyConstraint(["created_by"], ["devices.id"]),
        )
        op.create_index(
            "ix_build_input_snapshots_target",
            "build_input_snapshots",
            ["target", "created_at"],
        )
    columns = {column["name"] for column in inspector.get_columns("build_jobs")}
    if "source_snapshot_sha256" not in columns:
        op.add_column(
            "build_jobs",
            sa.Column("source_snapshot_sha256", sa.String(length=64), nullable=False, server_default=""),
        )
    if "input_snapshot_sha256" not in columns:
        op.add_column("build_jobs", sa.Column("input_snapshot_sha256", sa.String(length=64)))
    publication_columns = {column["name"] for column in inspector.get_columns("publications")}
    for name in ("build_id", "source_snapshot_sha256", "input_snapshot_sha256"):
        if name not in publication_columns:
            op.add_column("publications", sa.Column(name, sa.String(length=64)))


def downgrade() -> None:
    for name in ("input_snapshot_sha256", "source_snapshot_sha256", "build_id"):
        op.drop_column("publications", name)
    op.drop_column("build_jobs", "input_snapshot_sha256")
    op.drop_column("build_jobs", "source_snapshot_sha256")
    op.drop_index("ix_build_input_snapshots_target", table_name="build_input_snapshots")
    op.drop_table("build_input_snapshots")
