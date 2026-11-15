# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_note_replies.sources: what the answering agent may ground its answer in.

The sender of a reply chooses none, linked_pdf, library or library_and_mental_model; null means the
sender did not say (every reply stored before, and replies from clients that do not send it), and the
agent then uses everything as before. No backfill. The column is nullable and read by nothing older,
so the migration may run while the previous version still serves. See protocol/agent-notes.md
"Reply sources".

Revision ID: 0034_agent_note_reply_sources
Revises: 0033_push_registrations
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034_agent_note_reply_sources"
down_revision = "0033_push_registrations"
branch_labels = None
depends_on = None

TABLE = "agent_note_replies"


def columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    if "sources" not in columns():
        with op.batch_alter_table(TABLE) as batch:
            batch.add_column(sa.Column("sources", sa.String(length=32), nullable=True))


def downgrade() -> None:
    if "sources" in columns():
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column("sources")
