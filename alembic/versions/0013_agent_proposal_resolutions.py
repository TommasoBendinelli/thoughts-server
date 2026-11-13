# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add durable agent-proposal resolution jobs."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_agent_proposals"
down_revision = "0012_render_frontier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_proposal_resolutions" in set(inspector.get_table_names()):
        return
    op.create_table(
        "agent_proposal_resolutions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("device_id", sa.String(length=128), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("device_sequence", sa.Integer(), nullable=False),
        sa.Column("bundle_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("proposal_ids", sa.JSON(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("undo_of", sa.String(length=64)),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("device_id", "device_sequence"),
    )
    op.create_index(
        "ix_agent_proposal_resolutions_queue",
        "agent_proposal_resolutions",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_proposal_resolutions_queue",
        table_name="agent_proposal_resolutions",
    )
    op.drop_table("agent_proposal_resolutions")
