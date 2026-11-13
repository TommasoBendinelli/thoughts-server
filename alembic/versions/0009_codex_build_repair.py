# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add explicit Codex repair mode to build jobs."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_codex_build_repair"
down_revision = "0008_conditional_render_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("build_jobs")}
    if "repair_mode" not in columns:
        with op.batch_alter_table("build_jobs") as batch:
            batch.add_column(
                sa.Column(
                    "repair_mode",
                    sa.String(length=32),
                    nullable=False,
                    server_default="none",
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("build_jobs") as batch:
        batch.drop_column("repair_mode")
