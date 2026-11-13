# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Persist atomic source publication identities."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_source_publication_identity"
down_revision = "0003_annotation_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 historically creates the currently imported metadata on a fresh
    # database. Existing production databases still need this table here.
    if "source_publications" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "source_publications",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("device_id", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_frontier", sa.Integer(), nullable=False),
        sa.Column("results_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"]),
    )


def downgrade() -> None:
    if "source_publications" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("source_publications")
