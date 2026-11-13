# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add HTML editions: one per published PDF, with their background build queue.

Revision ID: 0024_html_editions
Revises: 0023_server_settings
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_html_editions"
down_revision = "0023_server_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "html_editions" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "html_editions",
        sa.Column("pdf_sha256", sa.String(length=64), primary_key=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("project", sa.String(length=512), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("source_format", sa.String(length=32), nullable=False),
        sa.Column("source_snapshot_sha256", sa.String(length=64), nullable=True),
        sa.Column("build_id", sa.String(length=64), nullable=True),
        sa.Column("input_ref", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("edition_sha256", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_html_editions_queue", "html_editions", ["status", "next_attempt_at", "created_at"])
    op.create_index("ix_html_editions_filename", "html_editions", ["filename", "created_at"])


def downgrade() -> None:
    if "html_editions" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_index("ix_html_editions_filename", table_name="html_editions")
        op.drop_index("ix_html_editions_queue", table_name="html_editions")
        op.drop_table("html_editions")
