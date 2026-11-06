"""Scope glossary terms to a connection.

Revision ID: 0020_glossary_connection
Revises: 0019_data_dictionary
Create Date: 2026-09-23

A tenant can have several connections whose business vocabulary differs,
so each glossary term now belongs to one connection and is deleted with
it. Existing rows keep connection_id NULL and continue to apply to every
connection (no guessing which connection they belonged to).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_glossary_connection"
down_revision: Union[str, None] = "0019_data_dictionary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("glossary_terms", sa.Column("connection_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_glossary_terms_connection_id",
        "glossary_terms",
        "connections",
        ["connection_id"],
        ["id"],
    )
    op.create_index("ix_glossary_terms_connection_id", "glossary_terms", ["connection_id"])
    op.drop_constraint("uq_glossary_terms_tenant_term", "glossary_terms", type_="unique")
    op.create_unique_constraint(
        "uq_glossary_terms_tenant_conn_term",
        "glossary_terms",
        ["tenant_id", "connection_id", "term"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_glossary_terms_tenant_conn_term", "glossary_terms", type_="unique")
    op.execute("DELETE FROM glossary_terms WHERE connection_id IS NOT NULL")
    op.create_unique_constraint(
        "uq_glossary_terms_tenant_term", "glossary_terms", ["tenant_id", "term"]
    )
    op.drop_index("ix_glossary_terms_connection_id", "glossary_terms")
    op.drop_constraint("fk_glossary_terms_connection_id", "glossary_terms", type_="foreignkey")
    op.drop_column("glossary_terms", "connection_id")
