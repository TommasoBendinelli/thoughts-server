# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent_notes.links: rectangles of a note's image that open a source when tapped.

See protocol/agent-notes.md "Links".

Revision ID: 0029_agent_note_links
Revises: 0028_review_card_pending_moves
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_agent_note_links"
down_revision = "0028_review_card_pending_moves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_notes")}
    if "links" not in columns:
        with op.batch_alter_table("agent_notes") as batch:
            batch.add_column(sa.Column("links", sa.JSON(), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_notes")}
    if "links" in columns:
        with op.batch_alter_table("agent_notes") as batch:
            batch.drop_column("links")
