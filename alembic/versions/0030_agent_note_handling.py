# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_note_handling and agent_note_reply_listeners.scope: who answers a reply or a page.

agent_note_handling records, per reply and per page the user sends, which live listeners
covered its note when it arrived and which agent claimed it (the first claim wins);
agent_note_reply_listeners.scope holds a topic owner's scope. See protocol/agent-notes.md
"Who answers" and "Topic owners".

Revision ID: 0030_agent_note_handling
Revises: 0029_agent_note_links
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030_agent_note_handling"
down_revision = "0029_agent_note_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_note_handling" not in set(inspector.get_table_names()):
        op.create_table(
            "agent_note_handling",
            sa.Column("item_id", sa.String(length=36), primary_key=True),
            sa.Column("kind", sa.String(length=8), nullable=False),
            sa.Column("note_id", sa.String(length=36), nullable=False),
            sa.Column("arrived_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("covered_by", sa.JSON(), nullable=True),
            sa.Column("claimed_device_id", sa.String(length=128), nullable=True),
            sa.Column("claimed_conversation_id", sa.String(length=128), nullable=True),
            sa.Column("claimed_role", sa.String(length=16), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("answer_note_id", sa.String(length=36), nullable=True),
        )
        op.create_index("ix_agent_note_handling_note", "agent_note_handling", ["note_id"])
    columns = {column["name"] for column in inspector.get_columns("agent_note_reply_listeners")}
    if "scope" not in columns:
        with op.batch_alter_table("agent_note_reply_listeners") as batch:
            batch.add_column(sa.Column("scope", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("agent_note_reply_listeners")}
    if "scope" in columns:
        op.execute(sa.text("DELETE FROM agent_note_reply_listeners WHERE note_id LIKE 'topic:%'"))
        with op.batch_alter_table("agent_note_reply_listeners") as batch:
            batch.drop_column("scope")
    if "agent_note_handling" in set(inspector.get_table_names()):
        op.drop_index("ix_agent_note_handling_note", table_name="agent_note_handling")
        op.drop_table("agent_note_handling")
