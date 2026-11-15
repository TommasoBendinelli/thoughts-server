# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add push_registrations: where each device receives content-free push pings.

One row per device, provider and app with the device's registration token (unique by its SHA-256, so a
token seen on another device moves there), and the outcome of the last send. Rows go with their device
(ON DELETE CASCADE). The table is new and read by nothing older, so the migration may run while the
previous version still serves. See protocol/push.md.

Revision ID: 0033_push_registrations
Revises: 0032_agent_note_wakers
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033_push_registrations"
down_revision = "0032_agent_note_wakers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "push_registrations" not in set(inspector.get_table_names()):
        op.create_table(
            "push_registrations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "device_id",
                sa.String(length=128),
                sa.ForeignKey("devices.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("provider", sa.String(length=16), nullable=False),
            sa.Column("app", sa.String(length=200), nullable=False),
            sa.Column("token", sa.Text(), nullable=False),
            sa.Column("token_sha256", sa.String(length=64), nullable=False, unique=True),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_result", sa.String(length=64), nullable=True),
            sa.UniqueConstraint("device_id", "provider", "app", name="uq_push_registrations_device_app"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "push_registrations" in set(inspector.get_table_names()):
        op.drop_table("push_registrations")
