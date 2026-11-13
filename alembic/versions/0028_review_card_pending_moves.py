# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add review_card_pending_moves: card moves that wait for an agent proposal's acceptance.

See protocol/review-card-pending-moves.md.

Revision ID: 0028_review_card_pending_moves
Revises: 0027_agent_note_reply_photos
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0028_review_card_pending_moves"
down_revision = "0027_agent_note_reply_photos"
branch_labels = None
depends_on = None

TABLE = "review_card_pending_moves"


def upgrade() -> None:
    if TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("card_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=128), nullable=False),
        sa.Column("source_path", sa.String(length=1024), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("target_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_review_card_pending_moves_state", TABLE, ["state", "proposal_id"])
    op.create_index("ix_review_card_pending_moves_card", TABLE, ["card_id", "state"])


def downgrade() -> None:
    if TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_index("ix_review_card_pending_moves_card", table_name=TABLE)
        op.drop_index("ix_review_card_pending_moves_state", table_name=TABLE)
        op.drop_table(TABLE)
