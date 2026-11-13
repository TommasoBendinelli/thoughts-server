# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add Fedora-owned Overleaf/Git provider synchronization.

Revision ID: 0019_provider_sync
Revises: 0018_deferred_proposal_renders
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_provider_sync"
down_revision = "0018_deferred_proposal_renders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "provider_projects" not in tables:
        op.create_table(
            "provider_projects",
            sa.Column("project", sa.String(length=255), primary_key=True),
            sa.Column("target", sa.String(length=255), nullable=False),
            sa.Column("remote", sa.String(length=255), nullable=False, server_default="origin"),
            sa.Column("branch", sa.String(length=255), nullable=False, server_default="main"),
            sa.Column(
                "source",
                sa.String(length=1024),
                nullable=False,
                server_default="tex/source/main.tex",
            ),
            sa.Column(
                "overleaf_root",
                sa.String(length=1024),
                nullable=False,
                server_default="overleaf",
            ),
            sa.Column("remote_url", sa.String(length=2048), nullable=True),
            sa.Column("checkout_root", sa.String(length=2048), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "check_interval_seconds",
                sa.Integer(),
                nullable=False,
                server_default="300",
            ),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="idle"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("provider_head", sa.String(length=64), nullable=True),
            sa.Column("canonical_frontier", sa.Integer(), nullable=True),
            sa.Column("checkpoint_json", sa.JSON(), nullable=False),
            sa.Column("render_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_provider_projects_next_check_at",
            "provider_projects",
            ["next_check_at"],
        )
    if "provider_sync_jobs" not in tables:
        op.create_table(
            "provider_sync_jobs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("trace_id", sa.String(length=36), nullable=True),
            sa.Column("project", sa.String(length=255), nullable=False),
            sa.Column(
                "direction",
                sa.String(length=32),
                nullable=False,
                server_default="bidirectional",
            ),
            sa.Column("trigger", sa.String(length=32), nullable=False, server_default="schedule"),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("provider_head", sa.String(length=64), nullable=True),
            sa.Column("canonical_frontier", sa.Integer(), nullable=True),
            sa.Column("build_id", sa.String(length=64), nullable=True),
            sa.Column("changed_paths_json", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["project"],
                ["provider_projects.project"],
                ondelete="CASCADE",
            ),
        )
        op.create_index(
            "ix_provider_sync_jobs_queue",
            "provider_sync_jobs",
            ["status", "created_at"],
        )
        op.create_index(
            "ix_provider_sync_jobs_project_created",
            "provider_sync_jobs",
            ["project", "created_at"],
        )
        op.create_index(
            "ix_provider_sync_jobs_trace_id",
            "provider_sync_jobs",
            ["trace_id"],
        )


def downgrade() -> None:
    op.drop_table("provider_sync_jobs")
    op.drop_table("provider_projects")