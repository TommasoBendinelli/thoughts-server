# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Add annotation, publication, and explicit source-deletion state."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0003_annotation_authority"
down_revision = "0002_operation_entity_revision"
branch_labels = None
depends_on = None

DOCUMENT_NAMESPACE = uuid.UUID("b545eae6-4e3c-46ab-b798-78fb30966d58")


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    publication_columns = _columns("publications")
    with op.batch_alter_table("publications") as batch:
        if "document_id" not in publication_columns:
            batch.add_column(sa.Column("document_id", sa.String(length=64), nullable=True))
        if "annotation_frontier" not in publication_columns:
            batch.add_column(
                sa.Column("annotation_frontier", sa.Integer(), nullable=False, server_default="0")
            )
        if "placement_manifest_sha256" not in publication_columns:
            batch.add_column(sa.Column("placement_manifest_sha256", sa.String(length=64)))
    publications = sa.table(
        "publications",
        sa.column("filename", sa.String),
        sa.column("document_id", sa.String),
        sa.column("comment_frontier", sa.Integer),
        sa.column("annotation_frontier", sa.Integer),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.select(publications.c.filename, publications.c.document_id)):
        if not row.document_id:
            connection.execute(
                publications.update()
                .where(publications.c.filename == row.filename)
                .values(document_id=str(uuid.uuid5(DOCUMENT_NAMESPACE, row.filename)))
            )
    connection.execute(
        publications.update().values(annotation_frontier=publications.c.comment_frontier)
    )
    with op.batch_alter_table("publications") as batch:
        batch.alter_column("document_id", existing_type=sa.String(length=64), nullable=False)
        batch.create_foreign_key(
            "fk_publications_placement_manifest",
            "objects",
            ["placement_manifest_sha256"],
            ["sha256"],
        )

    source_columns = _columns("source_files")
    if "deleted" not in source_columns:
        with op.batch_alter_table("source_files") as batch:
            batch.add_column(sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()))

    build_columns = _columns("build_jobs")
    if "annotation_frontier" not in build_columns:
        with op.batch_alter_table("build_jobs") as batch:
            batch.add_column(
                sa.Column("annotation_frontier", sa.Integer(), nullable=False, server_default="0")
            )
        jobs = sa.table(
            "build_jobs",
            sa.column("comment_frontier", sa.Integer),
            sa.column("annotation_frontier", sa.Integer),
        )
        connection.execute(jobs.update().values(annotation_frontier=jobs.c.comment_frontier))

    connection.execute(
        sa.text("UPDATE entities SET entity_type = 'annotation' WHERE entity_type = 'comment'")
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("UPDATE entities SET entity_type = 'comment' WHERE entity_type = 'annotation'")
    )
    with op.batch_alter_table("build_jobs") as batch:
        if "annotation_frontier" in _columns("build_jobs"):
            batch.drop_column("annotation_frontier")
    with op.batch_alter_table("source_files") as batch:
        if "deleted" in _columns("source_files"):
            batch.drop_column("deleted")
    with op.batch_alter_table("publications") as batch:
        columns = _columns("publications")
        if "placement_manifest_sha256" in columns:
            batch.drop_constraint("fk_publications_placement_manifest", type_="foreignkey")
            batch.drop_column("placement_manifest_sha256")
        if "annotation_frontier" in columns:
            batch.drop_column("annotation_frontier")
        if "document_id" in columns:
            batch.drop_column("document_id")
