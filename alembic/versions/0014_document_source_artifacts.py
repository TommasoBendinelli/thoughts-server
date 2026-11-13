# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Attach immutable per-document source artifacts to publications."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_doc_artifacts"
down_revision = "0013_agent_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("publications")}
    if "source_artifact_sha256" not in columns:
        op.add_column(
            "publications",
            sa.Column(
                "source_artifact_sha256",
                sa.String(length=64),
                sa.ForeignKey("objects.sha256"),
            ),
        )


def downgrade() -> None:
    op.drop_column("publications", "source_artifact_sha256")
