# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_note_reply_photos: a photo sent with a reply (e.g. from the glasses camera).

See protocol/agent-notes.md "Reply photos".

Revision ID: 0027_agent_note_reply_photos
Revises: 0026_agent_note_reply_listeners
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_agent_note_reply_photos"
down_revision = "0026_agent_note_reply_listeners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "agent_note_reply_photos" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "agent_note_reply_photos",
        sa.Column("reply_id", sa.String(length=36), sa.ForeignKey("agent_note_replies.id"), primary_key=True),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("image_sha256", sa.String(length=64), sa.ForeignKey("objects.sha256"), nullable=False),
        sa.Column("image_width", sa.Integer(), nullable=False),
        sa.Column("image_height", sa.Integer(), nullable=False),
        sa.Column("image_bytes", sa.Integer(), nullable=False),
        sa.Column("capture", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    if "agent_note_reply_photos" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("agent_note_reply_photos")
