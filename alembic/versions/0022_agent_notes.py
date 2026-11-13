# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add agent notes: visual notes an agent sends to the phone, and their replies.

See protocol/agent-notes.md.  Images and stroke documents live in ``objects``.

Revision ID: 0022_agent_notes
Revises: 0021_remove_provider_sync
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_agent_notes"
down_revision = "0021_remove_provider_sync"
branch_labels = None
depends_on = None

NOTE_INDEXES = {
    "ix_agent_notes_document": ["document_id", "sequence"],
    "ix_agent_notes_review_card": ["review_card_id", "sequence"],
    "ix_agent_notes_sender": ["sender_device_id", "sequence"],
    "ix_agent_notes_created": ["created_at"],
}
REPLY_INDEXES = {"ix_agent_note_replies_note": ["note_id", "sequence"]}


def _indexes(table: str) -> set[str]:
    return {value["name"] for value in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_notes" not in tables:
        op.create_table(
            "agent_notes",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("sender_device_id", sa.String(length=128), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("media_type", sa.String(length=32), nullable=False),
            sa.Column("image_sha256", sa.String(length=64), nullable=False),
            sa.Column("image_width", sa.Integer(), nullable=False),
            sa.Column("image_height", sa.Integer(), nullable=False),
            sa.Column("image_bytes", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.String(length=128), nullable=True),
            sa.Column("document_filename", sa.String(length=512), nullable=True),
            sa.Column("review_card_id", sa.String(length=128), nullable=True),
            sa.Column("review_question", sa.String(length=500), nullable=True),
            sa.Column("origin", sa.JSON(), nullable=True),
            sa.Column("content_sha256", sa.String(length=64), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_device_id", sa.String(length=128), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["image_sha256"], ["objects.sha256"]),
            sa.UniqueConstraint("id"),
        )
    existing = _indexes("agent_notes")
    for name, columns in NOTE_INDEXES.items():
        if name not in existing:
            op.create_index(name, "agent_notes", columns)

    if "agent_note_replies" not in tables:
        op.create_table(
            "agent_note_replies",
            sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("note_id", sa.String(length=36), nullable=False),
            sa.Column("device_id", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("media_type", sa.String(length=32), nullable=False),
            sa.Column("image_sha256", sa.String(length=64), nullable=False),
            sa.Column("image_width", sa.Integer(), nullable=False),
            sa.Column("image_height", sa.Integer(), nullable=False),
            sa.Column("image_bytes", sa.Integer(), nullable=False),
            sa.Column("strokes_sha256", sa.String(length=64), nullable=True),
            sa.Column("stroke_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("content_sha256", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(["note_id"], ["agent_notes.id"]),
            sa.ForeignKeyConstraint(["image_sha256"], ["objects.sha256"]),
            sa.ForeignKeyConstraint(["strokes_sha256"], ["objects.sha256"]),
            sa.UniqueConstraint("id"),
        )
    existing = _indexes("agent_note_replies")
    for name, columns in REPLY_INDEXES.items():
        if name not in existing:
            op.create_index(name, "agent_note_replies", columns)

    if "agent_note_listeners" not in tables:
        op.create_table(
            "agent_note_listeners",
            sa.Column("device_id", sa.String(length=128), primary_key=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("context", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "agent_note_listeners" in tables:
        op.drop_table("agent_note_listeners")
    if "agent_note_replies" in tables:
        for name in REPLY_INDEXES:
            if name in _indexes("agent_note_replies"):
                op.drop_index(name, table_name="agent_note_replies")
        op.drop_table("agent_note_replies")
    if "agent_notes" in tables:
        for name in NOTE_INDEXES:
            if name in _indexes("agent_notes"):
                op.drop_index(name, table_name="agent_notes")
        op.drop_table("agent_notes")
