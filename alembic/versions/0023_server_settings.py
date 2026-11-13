# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add server settings, starting with the Library's default-folder segment.

``default_folder_segment`` is ``default`` once the default-folder migration
(thoughts_server.migrate_default_folder) renamed every Project's
``.thoughts/projects/<h>/documents`` folder; a missing row means ``documents``.

Revision ID: 0023_server_settings
Revises: 0022_agent_notes
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_server_settings"
down_revision = "0022_agent_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "server_settings" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "server_settings",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    if "server_settings" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("server_settings")
