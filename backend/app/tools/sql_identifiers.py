"""Dialect-aware SQL identifier quoting for schema/table/column refs."""

from __future__ import annotations


def qualified_table(schema_name: str | None, table_name: str, db_type: str) -> str:
    """Return a dialect-quoted qualified table reference."""
    db = (db_type or "postgres").lower()
    bare = table_name.split(".")[-1]
    schema = schema_name or (table_name.split(".")[0] if "." in table_name else None)
    if schema and bare:
        if db == "mssql":
            return f"[{schema}].[{bare}]"
        return f'"{schema}"."{bare}"'
    if db == "mssql":
        return f"[{bare}]"
    return f'"{bare}"'


def qualified_column(schema_name: str | None, table_name: str, column: str, db_type: str) -> str:
    table = qualified_table(schema_name, table_name, db_type)
    col = column.strip()
    if (db_type or "").lower() == "mssql":
        return f"{table}.[{col}]"
    return f'{table}."{col}"'


def parse_qualified_ref(ref: str) -> tuple[str | None, str]:
    """Parse schema.table into (schema, bare_table)."""
    ref = (ref or "").strip()
    if "." in ref:
        schema, table = ref.split(".", 1)
        return schema, table
    return None, ref
