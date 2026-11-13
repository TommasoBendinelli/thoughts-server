# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Record the entity revision produced by every operation."""

import sqlalchemy as sa

from alembic import context, op

revision = "0002_operation_entity_revision"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("operations")}
    if "entity_revision" in columns:
        return
    op.add_column("operations", sa.Column("entity_revision", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE operations AS operation
        SET entity_revision = entity.revision
        FROM entities AS entity
        WHERE operation.entity_type = entity.entity_type
          AND operation.collection = entity.collection
          AND operation.entity_id = entity.entity_id
          AND operation.entity_revision IS NULL
        """
    )


def downgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("operations")}
    if "entity_revision" in columns:
        op.drop_column("operations", "entity_revision")
