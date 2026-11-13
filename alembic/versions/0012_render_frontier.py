# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Materialize the canonical render frontier."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_render_frontier"
down_revision = "0011_render_state_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "render_frontiers" not in set(inspector.get_table_names()):
        op.create_table(
            "render_frontiers",
            sa.Column("target", sa.String(length=255), primary_key=True),
            sa.Column("source_frontier", sa.Integer(), nullable=False),
            sa.Column("render_frontier", sa.Integer(), nullable=False),
        )
    op.execute(sa.text("DELETE FROM render_frontiers WHERE target = 'all'"))
    op.execute(
        sa.text(
            """
            INSERT INTO render_frontiers (target, source_frontier, render_frontier)
            SELECT
                'all',
                COALESCE(MAX(server_sequence) FILTER (
                    WHERE entity_type = 'source' AND outcome IN ('applied', 'merged')
                ), 0),
                COALESCE(MAX(server_sequence) FILTER (
                    WHERE entity_type = 'source' AND render_impact = 'pdf'
                        AND outcome IN ('applied', 'merged')
                ), 0)
            FROM operations
            """
        )
    )


def downgrade() -> None:
    op.drop_table("render_frontiers")
