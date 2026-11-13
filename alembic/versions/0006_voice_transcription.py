# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add durable voice transcription jobs."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_voice_transcription"
down_revision = "0005_build_input_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "transcription_jobs" in set(inspector.get_table_names()):
        return
    op.create_table(
        "transcription_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("capture_id", sa.String(length=512), nullable=False),
        sa.Column("collection", sa.String(length=512), nullable=False),
        sa.Column("input_operation_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("input_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("transcribing_operation_id", sa.String(length=64)),
        sa.Column("response_object_sha256", sa.String(length=64)),
        sa.Column("last_error", sa.Text()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["input_operation_id"], ["operations.operation_id"]),
    )
    op.create_index("ix_transcription_jobs_capture_id", "transcription_jobs", ["capture_id"])
    op.create_index(
        "ix_transcription_jobs_queue",
        "transcription_jobs",
        ["status", "next_attempt_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_transcription_jobs_queue", table_name="transcription_jobs")
    op.drop_index("ix_transcription_jobs_capture_id", table_name="transcription_jobs")
    op.drop_table("transcription_jobs")
