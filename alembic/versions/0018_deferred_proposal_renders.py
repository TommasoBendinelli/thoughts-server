# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add the debounced proposal render trigger.

Revision ID: 0018_deferred_proposal_renders
Revises: 0017_trace_events
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_deferred_proposal_renders"
down_revision = "0017_trace_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "deferred_proposal_renders" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "deferred_proposal_renders",
        sa.Column("target", sa.String(length=255), primary_key=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolution_ids_json", sa.JSON(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requested_by"], ["devices.id"]),
    )


def downgrade() -> None:
    op.drop_table("deferred_proposal_renders")
