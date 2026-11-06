"""Map platform source types to SQL compiler and safety dialects."""

from __future__ import annotations

from app.source_types import SourceType


def safety_dialect(db_type: str | None) -> str | None:
    """sqlglot dialect for SQLSafetyPipeline."""
    key = (db_type or "").strip().lower()
    if SourceType.is_file_backed(key):
        return "duckdb"
    return {
        "postgres": "postgres",
        "mysql": "mysql",
        "mssql": "tsql",
    }.get(key)


def compiler_db_type(db_type: str | None) -> str:
    """Dialect key for sql_compiler time/literal helpers.

    CSV/Excel run on DuckDB, which accepts Postgres DATE_TRUNC / INTERVAL syntax.
    """
    key = (db_type or "").strip().lower()
    if SourceType.is_file_backed(key) or key == "duckdb":
        return "postgres"
    return key or "postgres"


def uses_postgres_time_sql(db_type: str | None) -> bool:
    return compiler_db_type(db_type) == "postgres"
