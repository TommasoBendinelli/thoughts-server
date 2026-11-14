# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_notes.answer_by and agent_note_handling.answer_by: the agent the user chose to answer.

agent_notes.answer_by holds the user's choice of who answers the replies to a note (null: automatic);
agent_note_handling.answer_by is that choice as it was when a reply arrived. Both are nullable and
read by nothing older, so the migration may run while the previous version still serves. See
protocol/agent-notes.md "The user's choice".

Revision ID: 0031_agent_note_answer_by
Revises: 0030_agent_note_handling
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_agent_note_answer_by"
down_revision = "0030_agent_note_handling"
branch_labels = None
depends_on = None

TABLES = ("agent_notes", "agent_note_handling")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        if "answer_by" not in {column["name"] for column in inspector.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("answer_by", sa.JSON(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        if "answer_by" in {column["name"] for column in inspector.get_columns(table)}:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("answer_by")
