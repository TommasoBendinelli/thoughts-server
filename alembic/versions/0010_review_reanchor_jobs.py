# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add durable review-card re-anchoring jobs."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_review_reanchor_jobs"
down_revision = "0009_codex_build_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "review_reanchor_jobs" in set(inspector.get_table_names()):
        return
    op.create_table(
        "review_reanchor_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("card_id", sa.String(length=512), nullable=False),
        sa.Column("collection", sa.String(length=512), nullable=False),
        sa.Column("input_operation_id", sa.String(length=64), nullable=False),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("target_pdf_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("output_operation_id", sa.String(length=64)),
        sa.Column("last_error", sa.Text()),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["input_operation_id"], ["operations.operation_id"]),
        sa.ForeignKeyConstraint(["target_pdf_sha256"], ["objects.sha256"]),
        sa.UniqueConstraint("card_id", "target_pdf_sha256"),
    )
    op.create_index(
        "ix_review_reanchor_jobs_card_id",
        "review_reanchor_jobs",
        ["card_id"],
    )
    op.create_index(
        "ix_review_reanchor_jobs_queue",
        "review_reanchor_jobs",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_reanchor_jobs_queue", table_name="review_reanchor_jobs")
    op.drop_index("ix_review_reanchor_jobs_card_id", table_name="review_reanchor_jobs")
    op.drop_table("review_reanchor_jobs")
