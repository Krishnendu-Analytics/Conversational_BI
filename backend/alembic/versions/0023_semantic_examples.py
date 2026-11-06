"""Add semantic_examples and semantic_rules.origin.

Revision ID: 0023_semantic_examples
Revises: 0022_semantic_rules
Create Date: 2026-09-24

Example questions (imported or AI-drafted) let an admin evaluate the
assistant against a connection. ``origin`` marks AI-drafted rules.
Additive only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_semantic_examples"
down_revision: Union[str, None] = "0022_semantic_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "semantic_rules",
        sa.Column("origin", sa.String(length=20), nullable=False, server_default="import"),
    )
    op.create_table(
        "semantic_examples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("question_key", sa.String(length=300), nullable=False),
        sa.Column("tables_json", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("origin", sa.String(length=20), nullable=False, server_default="import"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "question_key", name="uq_semantic_examples_conn_question"
        ),
    )
    op.create_index("ix_semantic_examples_tenant_id", "semantic_examples", ["tenant_id"])
    op.create_index(
        "ix_semantic_examples_connection_id", "semantic_examples", ["connection_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_semantic_examples_connection_id", "semantic_examples")
    op.drop_index("ix_semantic_examples_tenant_id", "semantic_examples")
    op.drop_table("semantic_examples")
    op.drop_column("semantic_rules", "origin")
