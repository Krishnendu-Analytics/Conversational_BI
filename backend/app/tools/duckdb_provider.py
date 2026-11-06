"""DuckDB-backed provider for CSV/Excel file connections."""

from __future__ import annotations

import json
from pathlib import Path

from app.query_execution import QueryExecutionError, QueryExecutionLimits, fetch_bounded_rows
from app.tools.customer_db import ConnectionTestResult, DatabaseProvider
from app.tools.duckdb_engine import execute_duckdb_query, introspect_duckdb, parse_connection_manifest


class DuckDBFileProvider(DatabaseProvider):
    """Query engine over tenant-scoped DuckDB files."""

    def __init__(self, connection_url: str) -> None:
        super().__init__(connection_url)
        self._duckdb_path = parse_connection_manifest(connection_url)

    @property
    def duckdb_path(self) -> Path:
        return self._duckdb_path

    def test_connection(self) -> ConnectionTestResult:
        if not self._duckdb_path.exists():
            return ConnectionTestResult(
                ok=False,
                message="Dataset store not found",
                read_only_verified=True,
            )
        try:
            introspect_duckdb(self._duckdb_path)
            return ConnectionTestResult(
                ok=True,
                message="File dataset ready",
                read_only_verified=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionTestResult(
                ok=False,
                message=f"Dataset unavailable: {type(exc).__name__}",
                read_only_verified=True,
            )

    def list_schemas(self) -> list[str]:
        return ["main"]

    def default_schema(self) -> str:
        return "main"

    def introspect_schema(self, schemas: list[str] | None = None) -> dict:
        return introspect_duckdb(self._duckdb_path)

    def execute_read_only_query(
        self,
        sql: str,
        *,
        limits: QueryExecutionLimits | None = None,
        db_type: str | None = None,
    ) -> list[dict]:
        from app.tools.sql_safety import assert_read_only_sql

        assert_read_only_sql(sql, db_type=db_type or "csv")
        max_rows = limits.max_rows if limits else 10_000
        try:
            return execute_duckdb_query(self._duckdb_path, sql, max_rows=max_rows)
        except QueryExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001
            from app.agent.file_dashboard_sql import duckdb_user_message

            raise QueryExecutionError(
                str(exc),
                user_message=duckdb_user_message(exc),
            ) from exc


def is_file_connection_manifest(raw: str) -> bool:
    try:
        data = json.loads(raw)
        return bool(data.get("duckdb_path"))
    except (json.JSONDecodeError, TypeError):
        return False
