"""Scope semantic uniqueness to connection + physical source.

Revision ID: 0015_semantic_connection_identity
Revises: 0014_rag_documents

0011 added connection_id to metrics/dimensions/entities but never replaced the
tenant-global unique names created in 0003/0006. Discovery already inserts
per-connection, so a second dataset with the same inferred label crashes.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_semantic_connection_identity"
down_revision: Union[str, None] = "0014_rag_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE_CONSTRAINTS = (
    ("metrics", "uq_metrics_tenant_name", "uq_metrics_tenant_conn_name"),
    ("dimensions", "uq_dimensions_tenant_name", "uq_dimensions_tenant_conn_name"),
    ("semantic_entities", "uq_semantic_entities_tenant_name", "uq_semantic_entities_tenant_conn_name"),
)

_SOURCE_INDEXES = (
    (
        "metrics",
        "uq_metrics_tenant_conn_source_ref",
        ["tenant_id", "connection_id", "source_column_ref"],
        "source_column_ref IS NOT NULL",
        """
        DELETE FROM metrics AS a
        WHERE a.source_column_ref IS NOT NULL
          AND a.connection_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM metrics AS b
            WHERE b.tenant_id = a.tenant_id
              AND b.connection_id = a.connection_id
              AND b.source_column_ref = a.source_column_ref
              AND b.id < a.id
          )
        """,
    ),
    (
        "dimensions",
        "uq_dimensions_tenant_conn_source_ref",
        ["tenant_id", "connection_id", "source_column_ref"],
        "source_column_ref IS NOT NULL",
        """
        DELETE FROM dimensions AS a
        WHERE a.source_column_ref IS NOT NULL
          AND a.connection_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM dimensions AS b
            WHERE b.tenant_id = a.tenant_id
              AND b.connection_id = a.connection_id
              AND b.source_column_ref = a.source_column_ref
              AND b.id < a.id
          )
        """,
    ),
    (
        "semantic_entities",
        "uq_semantic_entities_tenant_conn_source_table",
        ["tenant_id", "connection_id", "source_table"],
        "source_table IS NOT NULL",
        """
        DELETE FROM semantic_entities AS a
        WHERE a.source_table IS NOT NULL
          AND a.connection_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM semantic_entities AS b
            WHERE b.tenant_id = a.tenant_id
              AND b.connection_id = a.connection_id
              AND b.source_table = a.source_table
              AND b.id < a.id
          )
        """,
    ),
)


def _constraint_names(inspector: sa.Inspector, table: str) -> set[str]:
    names = {c["name"] for c in inspector.get_unique_constraints(table) if c.get("name")}
    names.update(idx["name"] for idx in inspector.get_indexes(table) if idx.get("name"))
    return names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, old_name, new_name in _TABLE_CONSTRAINTS:
        existing = _constraint_names(inspector, table)
        if old_name in existing:
            op.drop_constraint(old_name, table, type_="unique")
        inspector = sa.inspect(bind)
        existing = _constraint_names(inspector, table)
        if new_name not in existing:
            op.create_unique_constraint(new_name, table, ["tenant_id", "connection_id", "name"])

    for table, index_name, columns, where, dedupe_sql in _SOURCE_INDEXES:
        bind.execute(sa.text(dedupe_sql))
        inspector = sa.inspect(bind)
        existing = _constraint_names(inspector, table)
        if index_name not in existing:
            op.create_index(
                index_name,
                table,
                columns,
                unique=True,
                postgresql_where=sa.text(where),
                sqlite_where=sa.text(where),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, index_name, _columns, _where, _dedupe in reversed(_SOURCE_INDEXES):
        existing = _constraint_names(inspector, table)
        if index_name in existing:
            op.drop_index(index_name, table_name=table)
        inspector = sa.inspect(bind)

    for table, old_name, new_name in reversed(_TABLE_CONSTRAINTS):
        existing = _constraint_names(inspector, table)
        if new_name in existing:
            op.drop_constraint(new_name, table, type_="unique")
        inspector = sa.inspect(bind)
        existing = _constraint_names(inspector, table)
        if old_name not in existing:
            op.create_unique_constraint(old_name, table, ["tenant_id", "name"])
