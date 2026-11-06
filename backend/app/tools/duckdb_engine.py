"""Load parsed tabular data into tenant-scoped DuckDB files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import duckdb

from app.services.file_storage import duckdb_path_for_connection


def _safe_table_name(dataset_id: UUID) -> str:
    return f"t_{str(dataset_id).replace('-', '_')}"


def build_connection_manifest(duckdb_path: Path) -> str:
    return json.dumps({"duckdb_path": str(duckdb_path)})


def parse_connection_manifest(raw: str) -> Path:
    data = json.loads(raw)
    path = data.get("duckdb_path")
    if not path:
        raise ValueError("Invalid file connection manifest")
    return Path(path)


def load_table_into_duckdb(
    duckdb_path: Path,
    *,
    dataset_id: UUID,
    columns: list[dict[str, Any]],
    rows: list[list[Any]],
) -> str:
    """Create/replace a dataset table and insert rows. Returns physical table name."""
    table = _safe_table_name(dataset_id)
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(duckdb_path))
    try:
        col_defs = ", ".join(f'"{c["internal_name"]}" {c["duckdb_type"]}' for c in columns)
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
        if rows:
            placeholders = ", ".join("?" for _ in columns)
            insert_sql = f'INSERT INTO "{table}" VALUES ({placeholders})'
            conn.executemany(insert_sql, rows)
    finally:
        conn.close()
    return table


def introspect_duckdb(duckdb_path: Path) -> dict[str, Any]:
    """Return schema metadata compatible with existing discovery format."""
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
        result_tables: list[dict[str, Any]] = []
        for (table_name,) in tables:
            cols = conn.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                ORDER BY ordinal_position
                """,
                [table_name],
            ).fetchall()
            columns = [
                {"name": name, "data_type": dtype, "nullable": True}
                for name, dtype in cols
            ]
            result_tables.append(
                {
                    "schema_name": "main",
                    "table_name": table_name,
                    "object_type": "table",
                    "columns": columns,
                    "foreign_keys": [],
                }
            )
        return {"tables": result_tables}
    finally:
        conn.close()


def execute_duckdb_query(
    duckdb_path: Path,
    sql: str,
    *,
    max_rows: int,
) -> list[dict[str, Any]]:
    conn = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        fetched = cur.fetchmany(max_rows + 1)
        if len(fetched) > max_rows:
            from app.query_execution import ResultTooLargeError

            raise ResultTooLargeError(
                f"Result exceeded max rows ({max_rows})",
            )
        return [dict(zip(columns, row)) for row in fetched]
    finally:
        conn.close()


def duckdb_path_for(tenant_id: UUID, connection_id: UUID) -> Path:
    return duckdb_path_for_connection(tenant_id, connection_id)
