"""Add data_dictionary_entries: business descriptions of tables/columns.

Revision ID: 0019_data_dictionary
Revises: 0018_conversation_connections
Create Date: 2026-09-23

Stored separately from schema_tables because schema rediscovery deletes
and rebuilds those rows; curated descriptions must survive it. Additive
only — nothing reads these rows unless some exist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_data_dictionary"
down_revision: Union[str, None] = "0018_conversation_connections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_dictionary_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("schema_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("table_name", sa.String(length=200), nullable=False),
        sa.Column("column_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False, server_default="import"),
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
            "connection_id",
            "schema_name",
            "table_name",
            "column_name",
            name="uq_data_dictionary_conn_table_column",
        ),
    )
    op.create_index(
        "ix_data_dictionary_entries_tenant_id", "data_dictionary_entries", ["tenant_id"]
    )
    op.create_index(
        "ix_data_dictionary_entries_connection_id",
        "data_dictionary_entries",
        ["connection_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_dictionary_entries_connection_id", "data_dictionary_entries")
    op.drop_index("ix_data_dictionary_entries_tenant_id", "data_dictionary_entries")
    op.drop_table("data_dictionary_entries")
