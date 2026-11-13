# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Classify render impact and persist conditional-render fingerprints."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_conditional_render_checks"
down_revision = "0007_annotation_anchor_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    operation_columns = {column["name"] for column in inspector.get_columns("operations")}
    if "render_impact" not in operation_columns:
        with op.batch_alter_table("operations") as batch:
            batch.add_column(
                sa.Column(
                    "render_impact",
                    sa.String(length=32),
                    nullable=False,
                    server_default="none",
                )
            )
        op.execute(
            sa.text(
                "UPDATE operations SET render_impact = CASE "
                "WHEN entity_type = 'source' AND provenance LIKE 'anchor-job:%' THEN 'anchor_only' "
                "WHEN entity_type = 'source' THEN 'pdf' ELSE 'none' END"
            )
        )
        op.create_index(
            "ix_operations_render_impact",
            "operations",
            ["render_impact", "server_sequence"],
        )

    inspector = sa.inspect(bind)
    build_columns = {column["name"] for column in inspector.get_columns("build_jobs")}
    if "render_fingerprint" not in build_columns:
        with op.batch_alter_table("build_jobs") as batch:
            batch.add_column(sa.Column("render_fingerprint", sa.String(length=64)))
            batch.create_index(
                "ix_build_jobs_render_fingerprint",
                ["render_fingerprint"],
                unique=False,
            )


def downgrade() -> None:
    with op.batch_alter_table("build_jobs") as batch:
        batch.drop_index("ix_build_jobs_render_fingerprint")
        batch.drop_column("render_fingerprint")
    with op.batch_alter_table("operations") as batch:
        batch.drop_index("ix_operations_render_impact")
        batch.drop_column("render_impact")
