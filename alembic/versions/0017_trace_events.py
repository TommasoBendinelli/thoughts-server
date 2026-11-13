# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add the durable live trace event journal.

Revision ID: 0017_trace_events
Revises: 0016_sync_trace
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_trace_events"
down_revision = "0016_sync_trace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "trace_streams" not in tables:
        op.create_table(
            "trace_streams",
            sa.Column("trace_id", sa.String(length=36), primary_key=True),
            sa.Column("owner_device_id", sa.String(length=128), nullable=False),
            sa.Column("target", sa.String(length=255), nullable=True),
            sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("server_state", sa.String(length=32), nullable=False, server_default="queued"),
            sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["owner_device_id"], ["devices.id"]),
        )
    stream_indexes = {value["name"] for value in sa.inspect(op.get_bind()).get_indexes("trace_streams")}
    if "ix_trace_streams_owner_updated" not in stream_indexes:
        op.create_index("ix_trace_streams_owner_updated", "trace_streams", ["owner_device_id", "updated_at"])
    if "ix_trace_streams_terminal_updated" not in stream_indexes:
        op.create_index("ix_trace_streams_terminal_updated", "trace_streams", ["terminal", "updated_at"])
    if "trace_events" not in tables:
        op.create_table(
            "trace_events",
            sa.Column("trace_id", sa.String(length=36), primary_key=True),
            sa.Column("sequence", sa.Integer(), primary_key=True),
            sa.Column("event_key", sa.String(length=255), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("component", sa.String(length=64), nullable=False),
            sa.Column("phase", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("target", sa.String(length=255), nullable=True),
            sa.Column("build_id", sa.String(length=64), nullable=True),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("completed", sa.Integer(), nullable=True),
            sa.Column("total", sa.Integer(), nullable=True),
            sa.Column("unit", sa.String(length=32), nullable=True),
            sa.Column("bytes_completed", sa.Integer(), nullable=True),
            sa.Column("bytes_total", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(length=128), nullable=True),
            sa.Column("details_json", sa.JSON(), nullable=False),
            sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.ForeignKeyConstraint(["trace_id"], ["trace_streams.trace_id"], ondelete="CASCADE"),
            sa.UniqueConstraint("trace_id", "event_key", name="uq_trace_events_trace_event_key"),
        )
    event_indexes = {value["name"] for value in sa.inspect(op.get_bind()).get_indexes("trace_events")}
    if "ix_trace_events_occurred" not in event_indexes:
        op.create_index("ix_trace_events_occurred", "trace_events", ["occurred_at"])
    if "ix_trace_events_terminal_occurred" not in event_indexes:
        op.create_index("ix_trace_events_terminal_occurred", "trace_events", ["terminal", "occurred_at"])
    if "trace_build_links" not in tables:
        op.create_table(
            "trace_build_links",
            sa.Column("trace_id", sa.String(length=36), primary_key=True),
            sa.Column("build_id", sa.String(length=64), primary_key=True),
            sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["trace_id"], ["trace_streams.trace_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["build_id"], ["build_jobs.id"], ondelete="CASCADE"),
        )
    link_indexes = {value["name"] for value in sa.inspect(op.get_bind()).get_indexes("trace_build_links")}
    if "ix_trace_build_links_build_id" not in link_indexes:
        op.create_index("ix_trace_build_links_build_id", "trace_build_links", ["build_id"])


def downgrade() -> None:
    op.drop_index("ix_trace_build_links_build_id", table_name="trace_build_links")
    op.drop_table("trace_build_links")
    op.drop_index("ix_trace_events_terminal_occurred", table_name="trace_events")
    op.drop_index("ix_trace_events_occurred", table_name="trace_events")
    op.drop_table("trace_events")
    op.drop_index("ix_trace_streams_terminal_updated", table_name="trace_streams")
    op.drop_index("ix_trace_streams_owner_updated", table_name="trace_streams")
    op.drop_table("trace_streams")
