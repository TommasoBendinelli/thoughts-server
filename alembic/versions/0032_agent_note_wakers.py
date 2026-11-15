# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_note_wakers: the reply wakers and the sessions each can wake.

A reply waker runs on one machine and wakes that machine's Claude Code sessions when a reply, a page or a
capture is theirs. Its heartbeat keeps one row per device and machine, with the sessions it can wake. The
table is new and read by nothing older, so the migration may run while the previous version still serves.
See protocol/agent-notes.md "Wakers".

Revision ID: 0032_agent_note_wakers
Revises: 0031_agent_note_answer_by
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_agent_note_wakers"
down_revision = "0031_agent_note_answer_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_note_wakers" not in set(inspector.get_table_names()):
        op.create_table(
            "agent_note_wakers",
            sa.Column("device_id", sa.String(length=128), primary_key=True),
            sa.Column("machine", sa.String(length=64), primary_key=True),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("info", sa.JSON(), nullable=True),
            sa.Column("sessions", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "agent_note_wakers" in set(inspector.get_table_names()):
        op.drop_table("agent_note_wakers")
