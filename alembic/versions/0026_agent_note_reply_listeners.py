# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_note_reply_listeners: agents that wait on the reply feed to act on replies.

See protocol/agent-notes.md "Who hears a reply".

Revision ID: 0026_agent_note_reply_listeners
Revises: 0025_agent_proposal_authoring
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_agent_note_reply_listeners"
down_revision = "0025_agent_proposal_authoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "agent_note_reply_listeners" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "agent_note_reply_listeners",
        sa.Column("device_id", sa.String(length=128), primary_key=True),
        sa.Column("conversation_id", sa.String(length=128), primary_key=True),
        sa.Column("note_id", sa.String(length=36), primary_key=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if "agent_note_reply_listeners" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("agent_note_reply_listeners")
