"""Drop glossary_terms: business glossary removed in favour of the data dictionary.

Revision ID: 0021_drop_glossary
Revises: 0020_glossary_connection
Create Date: 2026-09-23

Table and column meaning now lives in data_dictionary_entries (per
connection, matched to the real schema). Any stored glossary terms are
discarded; downgrade recreates the empty table as it was after 0020.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_drop_glossary"
down_revision: Union[str, None] = "0020_glossary_connection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("glossary_terms")


def downgrade() -> None:
    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("term", sa.String(length=120), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["connections.id"], name="fk_glossary_terms_connection_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "connection_id", "term", name="uq_glossary_terms_tenant_conn_term"
        ),
    )
    op.create_index("ix_glossary_terms_tenant_id", "glossary_terms", ["tenant_id"])
    op.create_index("ix_glossary_terms_connection_id", "glossary_terms", ["connection_id"])
