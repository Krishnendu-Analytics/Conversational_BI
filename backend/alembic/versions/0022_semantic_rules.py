"""Add semantic_rules: business rules, agent settings and vocabulary.

Revision ID: 0022_semantic_rules
Revises: 0021_drop_glossary
Create Date: 2026-09-24

Imported per connection from a semantic-model workbook and shown to the
SQL prompts. Additive only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_semantic_rules"
down_revision: Union[str, None] = "0021_drop_glossary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "semantic_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("rule_key", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("category", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("rule", sa.Text(), nullable=False),
        sa.Column("guidance", sa.Text(), nullable=False, server_default=""),
        sa.Column("terms_json", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "kind", "rule_key", name="uq_semantic_rules_conn_kind_key"
        ),
    )
    op.create_index("ix_semantic_rules_tenant_id", "semantic_rules", ["tenant_id"])
    op.create_index("ix_semantic_rules_connection_id", "semantic_rules", ["connection_id"])


def downgrade() -> None:
    op.drop_index("ix_semantic_rules_connection_id", "semantic_rules")
    op.drop_index("ix_semantic_rules_tenant_id", "semantic_rules")
    op.drop_table("semantic_rules")
