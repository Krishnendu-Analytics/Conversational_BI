"""Semantic layer suggestions and extended metadata.

Revision ID: 0006_semantic_suggestions
Revises: 0005_schema_profiling
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_semantic_suggestions"
down_revision: Union[str, None] = "0005_schema_profiling"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("metrics", "dimensions"):
        op.add_column(table, sa.Column("label", sa.String(length=200), nullable=True))
        op.add_column(table, sa.Column("description", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("format", sa.String(length=40), nullable=True))
        op.add_column(table, sa.Column("synonyms_json", sa.Text(), nullable=True))
        op.add_column(
            table,
            sa.Column("status", sa.String(length=20), nullable=False, server_default="confirmed"),
        )
        op.add_column(table, sa.Column("source_column_ref", sa.String(length=300), nullable=True))

    op.create_table(
        "semantic_entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_table", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="suggested"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_semantic_entities_tenant_name"),
    )
    op.create_index(
        op.f("ix_semantic_entities_tenant_id"),
        "semantic_entities",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "semantic_relationships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("from_entity", sa.String(length=200), nullable=False),
        sa.Column("to_entity", sa.String(length=200), nullable=False),
        sa.Column("relationship_type", sa.String(length=40), nullable=False, server_default="many_to_one"),
        sa.Column("source_fk_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="suggested"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_semantic_relationships_tenant_id"),
        "semantic_relationships",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_semantic_relationships_tenant_id"), table_name="semantic_relationships")
    op.drop_table("semantic_relationships")
    op.drop_index(op.f("ix_semantic_entities_tenant_id"), table_name="semantic_entities")
    op.drop_table("semantic_entities")
    for table in ("dimensions", "metrics"):
        op.drop_column(table, "source_column_ref")
        op.drop_column(table, "status")
        op.drop_column(table, "synonyms_json")
        op.drop_column(table, "format")
        op.drop_column(table, "description")
        op.drop_column(table, "label")
