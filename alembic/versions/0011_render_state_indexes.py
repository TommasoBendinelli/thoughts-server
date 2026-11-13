# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add constant-time render-state lookup indexes."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_render_state_indexes"
down_revision = "0010_review_reanchor_jobs"
branch_labels = None
depends_on = None


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {row["name"] for row in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    build_indexes = _index_names(inspector, "build_jobs")
    if "ix_build_jobs_target_created" not in build_indexes:
        op.create_index(
            "ix_build_jobs_target_created",
            "build_jobs",
            ["target", "created_at"],
        )
    operation_indexes = _index_names(inspector, "operations")
    if "ix_operations_render_frontier" not in operation_indexes:
        op.create_index(
            "ix_operations_render_frontier",
            "operations",
            ["entity_type", "render_impact", "outcome", "server_sequence"],
        )


def downgrade() -> None:
    op.drop_index("ix_operations_render_frontier", table_name="operations")
    op.drop_index("ix_build_jobs_target_created", table_name="build_jobs")
