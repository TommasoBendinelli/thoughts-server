# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Permanent document registrations independent of published PDF versions."""

import sqlalchemy as sa

from alembic import op

revision = "0020_document_identity"
down_revision = "0019_provider_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "document_identity_migrations" not in tables:
        op.create_table(
            "document_identity_migrations",
            sa.Column("name", sa.String(64), primary_key=True),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    if "document_identities" not in tables:
        op.create_table(
            "document_identities",
            sa.Column("document_id", sa.String(36), primary_key=True),
            sa.Column("filename", sa.String(255), unique=True),
            sa.Column("source_path", sa.String(1024), unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("document_identities")
    op.drop_table("document_identity_migrations")
