"""Schema discovery orchestration — synchronously testable core."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.encryption import CredentialEncryption
from app.models import Connection, SchemaTable
from app.redis_client import schema_cache
from app.schema_profiling import profile_table
from app.schemas import SchemaTablePayload
from app.services.data_dictionary import apply_to_schema, load_entries
from app.services.semantic_model_import import (
    apply_relationships_to_schema,
    load_confirmed_relationships,
)
from app.tools.customer_db import ProviderFactory, resolve_discover_schemas


@dataclass
class DiscoveryResult:
    connection_id: UUID
    db_type: str
    tables: list[dict[str, Any]]
    schemas_used: list[str]
    status: str  # completed | partial | failed
    warnings: list[str] = field(default_factory=list)
    tables_discovered: int = 0


def _get_connection(db: Session, connection_id: UUID, tenant_id: UUID) -> Connection:
    conn = db.scalar(
        select(Connection).where(
            Connection.id == connection_id,
            Connection.tenant_id == tenant_id,
        )
    )
    if conn is None:
        raise ValueError("Connection not found")
    return conn


def _bare_table_name(table_name: str) -> str:
    return table_name.split(".")[-1]


def run_schema_discovery(
    db: Session,
    *,
    connection_id: UUID,
    tenant_id: UUID,
    scope: str = "default",
    schemas: list[str] | None = None,
    profile: bool = False,
    include_views: bool = False,
) -> DiscoveryResult:
    """Synchronously discover schema metadata. Never elevates DB privileges."""
    conn = _get_connection(db, connection_id, tenant_id)
    warnings: list[str] = []
    encryptor = CredentialEncryption()
    url = encryptor.decrypt(conn.encrypted_url)
    provider = ProviderFactory.create(conn.db_type, url)

    try:
        target_schemas = resolve_discover_schemas(provider, scope=scope, schemas=schemas)
    except ValueError as exc:
        return DiscoveryResult(
            connection_id=conn.id,
            db_type=conn.db_type,
            tables=[],
            schemas_used=[],
            status="failed",
            warnings=[str(exc)],
        )

    discovered_tables: list[dict[str, Any]] = []
    schemas_used: list[str] = []

    for schema_name in target_schemas:
        try:
            batch = provider.introspect_schema([schema_name])
            tables = batch.get("tables") or []
            if not include_views:
                tables = [t for t in tables if t.get("object_type", "table") != "view"]
            discovered_tables.extend(tables)
            if schema_name not in schemas_used:
                schemas_used.append(schema_name)
        except Exception as exc:  # noqa: BLE001 — partial discover
            warnings.append(f"Schema '{schema_name}' skipped: {type(exc).__name__}: {exc}")

    status = "completed"
    if warnings and discovered_tables:
        status = "partial"
    elif warnings and not discovered_tables:
        status = "failed"

    if discovered_tables:
        db.execute(
            delete(SchemaTable).where(
                SchemaTable.connection_id == conn.id,
                SchemaTable.tenant_id == tenant_id,
            )
        )
        for table in discovered_tables:
            schema_name = table.get("schema_name")
            bare = _bare_table_name(str(table.get("table_name") or ""))
            payload = SchemaTablePayload.model_validate(
                {
                    "columns": table.get("columns") or [],
                    "foreign_keys": table.get("foreign_keys") or [],
                    "schema_name": schema_name,
                }
            )
            profiling: dict[str, Any] = {}
            if profile:
                profiling = profile_table(
                    provider,
                    db_type=conn.db_type,
                    schema_name=schema_name,
                    table_name=bare,
                    columns=table.get("columns") or [],
                )
                table["estimated_row_count"] = profiling.get("estimated_row_count")
                table["profiling"] = {
                    "column_samples": profiling.get("column_samples", {}),
                    "column_stats": profiling.get("column_stats", {}),
                    "part_whole_pairs": profiling.get("part_whole_pairs") or [],
                }
            db.add(
                SchemaTable(
                    tenant_id=tenant_id,
                    connection_id=conn.id,
                    schema_name=schema_name,
                    table_name=bare,
                    columns_json=payload.model_dump_json(),
                    estimated_row_count=profiling.get("estimated_row_count"),
                    profiling_json=json.dumps(table.get("profiling") or {}, default=str)
                    if profile
                    else None,
                )
            )

    conn.last_discover_status = status
    conn.last_discover_at = datetime.now(timezone.utc)
    db.commit()

    result_payload = {
        "connection_id": conn.id,
        "db_type": conn.db_type,
        "tables": discovered_tables,
        "schemas_used": schemas_used,
        "status": status,
        "warnings": warnings,
    }
    if discovered_tables:
        # Curated descriptions and approved joins live outside schema_tables
        # and survive rediscovery; merge them back before caching.
        apply_to_schema(result_payload, load_entries(db, tenant_id, conn.id))
        apply_relationships_to_schema(
            result_payload, load_confirmed_relationships(db, tenant_id, conn.id)
        )
        schema_cache.set(tenant_id, conn.id, result_payload)

    return DiscoveryResult(
        connection_id=conn.id,
        db_type=conn.db_type,
        tables=discovered_tables,
        schemas_used=schemas_used,
        status=status,
        warnings=warnings,
        tables_discovered=len(discovered_tables),
    )


def get_stored_schema_for_tenant(
    db: Session,
    connection_id: UUID,
    tenant_id: UUID,
) -> dict[str, Any]:
    """Load persisted schema with tenant guard."""
    conn = _get_connection(db, connection_id, tenant_id)
    rows = db.scalars(
        select(SchemaTable)
        .where(
            SchemaTable.connection_id == conn.id,
            SchemaTable.tenant_id == tenant_id,
        )
        .order_by(SchemaTable.schema_name, SchemaTable.table_name)
    ).all()

    tables: list[dict] = []
    schemas_used: list[str] = []
    for row in rows:
        payload = SchemaTablePayload.model_validate_json(row.columns_json)
        schema_name = row.schema_name or payload.schema_name
        bare_name = row.table_name
        if schema_name and schema_name not in schemas_used:
            schemas_used.append(schema_name)
        tables.append(
            {
                "schema_name": schema_name,
                "table_name": bare_name,
                "columns": [col.model_dump() for col in payload.columns],
                "foreign_keys": [fk.model_dump() for fk in payload.foreign_keys],
                "estimated_row_count": row.estimated_row_count,
                "profiling": json.loads(row.profiling_json) if row.profiling_json else None,
            }
        )

    schema = {
        "connection_id": conn.id,
        "db_type": conn.db_type,
        "tables": tables,
        "schemas_used": schemas_used,
    }
    apply_to_schema(schema, load_entries(db, tenant_id, conn.id))
    return apply_relationships_to_schema(
        schema, load_confirmed_relationships(db, tenant_id, conn.id)
    )
