# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add server-side agent proposal authoring requests (proposal_authoring).

Revision ID: 0025_agent_proposal_authoring
Revises: 0024_html_editions
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_agent_proposal_authoring"
down_revision = "0024_html_editions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "agent_proposal_authoring" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "agent_proposal_authoring",
        sa.Column("request_id", sa.String(length=36), primary_key=True),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=32), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("publication_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if "agent_proposal_authoring" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("agent_proposal_authoring")
