"""Schema discovery runs, connection discover config, semantic connection scope.

Revision ID: 0011_schema_discovery_semantic_scope
Revises: 0010_conversation_analysis_state
Create Date: 2026-09-01
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_schema_discovery_semantic_scope"
down_revision: Union[str, None] = "0010_conversation_analysis_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_refs(text: str | None) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)", text))


def _qualified_names_for_row(table_name: str, columns_json: str) -> set[str]:
    names: set[str] = set()
    schema_name = None
    try:
        payload = json.loads(columns_json or "{}")
        schema_name = payload.get("schema_name")
    except json.JSONDecodeError:
        pass
    bare = table_name.split(".")[-1]
    if schema_name:
        names.add(f"{schema_name}.{bare}")
    if "." in table_name:
        names.add(table_name)
    names.add(bare)
    return names


def _match_connection(bind, tenant_id: str, refs: set[str]) -> str | None:
    if not refs:
        return None
    rows = bind.execute(
        sa.text(
            "SELECT connection_id, table_name, columns_json FROM schema_tables WHERE tenant_id = :tid"
        ),
        {"tid": tenant_id},
    ).fetchall()
    by_connection: dict[str, set[str]] = {}
    for connection_id, table_name, columns_json in rows:
        cid = str(connection_id)
        by_connection.setdefault(cid, set()).update(
            _qualified_names_for_row(table_name, columns_json)
        )
    candidates = [
        cid for cid, names in by_connection.items() if refs.issubset(names)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def upgrade() -> None:
    # This revision id is 36 chars; Alembic's default version_num is VARCHAR(32).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)"
        )
    op.create_table(
        "schema_discovery_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False, server_default="default"),
        sa.Column("schemas_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("warnings_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("tables_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schema_discovery_runs_connection", "schema_discovery_runs", ["connection_id"])

    op.create_table(
        "semantic_migration_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=40), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=False),
        sa.Column("object_name", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    for col, typ, default in (
        ("discover_scope", sa.String(20), "default"),
        ("discover_schemas_json", sa.Text(), "[]"),
        ("include_views", sa.Boolean(), sa.false()),
        ("profile_on_discover", sa.Boolean(), sa.false()),
        ("last_discover_status", sa.String(20), None),
        ("last_discover_at", sa.DateTime(timezone=True), None),
    ):
        kwargs = {"nullable": default is None}
        if default is not None and default is not sa.false():
            kwargs["server_default"] = default
        elif default is sa.false():
            kwargs["server_default"] = sa.false()
            kwargs["nullable"] = False
        op.add_column("connections", sa.Column(col, typ, **kwargs))

    op.add_column("schema_tables", sa.Column("schema_name", sa.String(length=200), nullable=True))

    for table in ("metrics", "dimensions", "semantic_entities", "semantic_relationships"):
        op.add_column(table, sa.Column("connection_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_connection_id",
            table,
            "connections",
            ["connection_id"],
            ["id"],
        )

    for table in ("metrics", "dimensions"):
        op.add_column(table, sa.Column("physical_schema", sa.String(200), nullable=True))
        op.add_column(table, sa.Column("physical_table", sa.String(200), nullable=True))
        op.add_column(table, sa.Column("physical_column", sa.String(200), nullable=True))
        op.add_column(
            table,
            sa.Column("mapping_status", sa.String(20), nullable=False, server_default="mapped"),
        )

    op.add_column("semantic_entities", sa.Column("physical_schema", sa.String(200), nullable=True))
    op.add_column("semantic_entities", sa.Column("physical_table", sa.String(200), nullable=True))
    op.add_column(
        "semantic_entities",
        sa.Column("mapping_status", sa.String(20), nullable=False, server_default="mapped"),
    )
    op.add_column(
        "semantic_relationships",
        sa.Column("mapping_status", sa.String(20), nullable=False, server_default="mapped"),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, table_name, columns_json FROM schema_tables")).fetchall()
    for row_id, table_name, columns_json in rows:
        schema_name = None
        try:
            schema_name = json.loads(columns_json or "{}").get("schema_name")
        except json.JSONDecodeError:
            pass
        if schema_name is None and "." in (table_name or ""):
            schema_name = table_name.split(".", 1)[0]
        if schema_name:
            bind.execute(
                sa.text("UPDATE schema_tables SET schema_name = :sn WHERE id = :id"),
                {"sn": schema_name, "id": row_id},
            )

    semantic_specs = [
        ("metrics", "metric", ("name", "expression", "source_column_ref", None)),
        ("dimensions", "dimension", ("name", "expression", "source_column_ref", None)),
        ("semantic_entities", "entity", ("name", None, None, "source_table")),
        (
            "semantic_relationships",
            "relationship",
            ("from_entity", None, None, "source_fk_json"),
        ),
    ]
    for table, obj_type, cols in semantic_specs:
        name_col, expr_col, src_col, extra_col = cols
        select_cols = ["id", "tenant_id", name_col]
        if expr_col:
            select_cols.append(expr_col)
        if src_col:
            select_cols.append(src_col)
        if extra_col:
            select_cols.append(extra_col)
        objs = bind.execute(sa.text(f"SELECT {', '.join(select_cols)} FROM {table}")).fetchall()
        for obj in objs:
            obj_id = obj[0]
            tenant_id = str(obj[1])
            name = obj[2]
            refs: set[str] = set()
            idx = 3
            if expr_col:
                refs |= _table_refs(obj[idx])
                idx += 1
            if src_col:
                refs |= _table_refs(obj[idx])
                idx += 1
            if extra_col:
                val = obj[idx]
                if extra_col == "source_fk_json":
                    try:
                        fk = json.loads(val or "{}")
                        rt = fk.get("referred_table")
                        if rt:
                            refs.add(rt)
                    except json.JSONDecodeError:
                        pass
                else:
                    refs |= _table_refs(val)
            matched = _match_connection(bind, tenant_id, refs)
            if matched:
                bind.execute(
                    sa.text(f"UPDATE {table} SET connection_id = :cid WHERE id = :id"),
                    {"cid": matched, "id": obj_id},
                )
            elif refs:
                bind.execute(
                    sa.text(f"UPDATE {table} SET mapping_status = 'unmapped' WHERE id = :id"),
                    {"id": obj_id},
                )
                bind.execute(
                    sa.text(
                        """
                        INSERT INTO semantic_migration_reports
                        (id, tenant_id, object_type, object_id, object_name, reason, metadata_json)
                        VALUES (:id, :tid, :otype, :oid, :oname, :reason, :meta)
                        """
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "tid": tenant_id,
                        "otype": obj_type,
                        "oid": str(obj_id),
                        "oname": name,
                        "reason": (
                            "Could not uniquely map semantic object to a single connection "
                            "from physical metadata."
                        ),
                        "meta": json.dumps({"refs": sorted(refs)}),
                    },
                )


def downgrade() -> None:
    op.drop_column("semantic_relationships", "mapping_status")
    op.drop_constraint("fk_semantic_relationships_connection_id", "semantic_relationships", type_="foreignkey")
    op.drop_column("semantic_relationships", "connection_id")
    op.drop_column("semantic_entities", "mapping_status")
    op.drop_column("semantic_entities", "physical_table")
    op.drop_column("semantic_entities", "physical_schema")
    op.drop_constraint("fk_semantic_entities_connection_id", "semantic_entities", type_="foreignkey")
    op.drop_column("semantic_entities", "connection_id")
    for table in ("dimensions", "metrics"):
        op.drop_column(table, "mapping_status")
        op.drop_column(table, "physical_column")
        op.drop_column(table, "physical_table")
        op.drop_column(table, "physical_schema")
        op.drop_constraint(f"fk_{table}_connection_id", table, type_="foreignkey")
        op.drop_column(table, "connection_id")
    op.drop_column("schema_tables", "schema_name")
    for col in (
        "last_discover_at",
        "last_discover_status",
        "profile_on_discover",
        "include_views",
        "discover_schemas_json",
        "discover_scope",
    ):
        op.drop_column("connections", col)
    op.drop_table("semantic_migration_reports")
    op.drop_index("ix_schema_discovery_runs_connection", table_name="schema_discovery_runs")
    op.drop_table("schema_discovery_runs")
