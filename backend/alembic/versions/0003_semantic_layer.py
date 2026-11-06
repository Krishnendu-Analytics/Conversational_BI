"""Add dimensions + glossary_terms; unique metric names per tenant.

Revision ID: 0003_semantic_layer
Revises: 0002_refresh_tokens
Create Date: 2026-08-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_semantic_layer"
down_revision: Union[str, None] = "0002_refresh_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_metrics_tenant_name",
        "metrics",
        ["tenant_id", "name"],
    )

    op.create_table(
        "dimensions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("expression", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_dimensions_tenant_name"),
    )
    op.create_index(op.f("ix_dimensions_tenant_id"), "dimensions", ["tenant_id"], unique=False)

    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.String(length=120), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "term", name="uq_glossary_terms_tenant_term"),
    )
    op.create_index(
        op.f("ix_glossary_terms_tenant_id"),
        "glossary_terms",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_glossary_terms_tenant_id"), table_name="glossary_terms")
    op.drop_table("glossary_terms")
    op.drop_index(op.f("ix_dimensions_tenant_id"), table_name="dimensions")
    op.drop_table("dimensions")
    op.drop_constraint("uq_metrics_tenant_name", "metrics", type_="unique")
