"""Apply semantic-validated dashboard filters to widget SQL at execution time."""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.agent.query_plan import FilterSpec
from app.agent.sql_compiler import (
    _catalog_maps,
    _filter_clause,
    _resolve_expression,
)
from app.tools.sql_safety import _parse_sql, _sqlglot_dialect


def validate_dashboard_filters(
    filters: list[dict[str, Any]] | list[Any],
    semantic_catalog: dict[str, Any] | None,
) -> list[FilterSpec]:
    """Validate filter fields against tenant semantic dimensions."""
    _, dimensions = _catalog_maps(semantic_catalog)
    if not dimensions and filters:
        raise ValueError("No semantic dimensions configured for dashboard filters")

    validated: list[FilterSpec] = []
    for raw in filters:
        if not isinstance(raw, dict):
            raise ValueError("Invalid filter payload")
        spec = FilterSpec.model_validate(raw)
        if spec.field.lower() not in dimensions:
            raise ValueError(f"Unknown dimension: {spec.field}")
        validated.append(spec)
    return validated


def build_filter_predicates(
    filters: list[FilterSpec],
    semantic_catalog: dict[str, Any] | None,
    db_type: str,
) -> list[tuple[str, str, str]]:
    """Return (field, resolved_expression, predicate_sql) for each filter."""
    _, dimensions = _catalog_maps(semantic_catalog)
    predicates: list[tuple[str, str, str]] = []
    for spec in filters:
        resolved = _resolve_expression(spec.field, {}, dimensions)
        if not resolved:
            raise ValueError(f"Unknown dimension: {spec.field}")
        predicate = _filter_clause(spec, resolved, db_type)
        predicates.append((spec.field, resolved, predicate))
    return predicates


def _tables_in_sql(widget_sql: str, db_type: str) -> set[str]:
    dialect = _sqlglot_dialect(db_type)
    try:
        statement = _parse_sql(widget_sql, dialect)
    except Exception:
        return set()

    tables: set[str] = set()
    for table in statement.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        schema_part = str(getattr(table, "db", None) or "").lower()
        bare = str(name).lower()
        qualified = f"{schema_part}.{bare}" if schema_part else bare
        tables.add(qualified)
        tables.add(bare)
    return tables


def _expression_references_widget(
    resolved_expression: str,
    widget_sql: str,
    schema: dict[str, Any],
    db_type: str,
    *,
    source_table: str | None = None,
) -> bool:
    """Return True when the dimension expression can apply to this widget query."""
    expr_lower = resolved_expression.lower()
    widget_tables = _tables_in_sql(widget_sql, db_type)

    for table in widget_tables:
        if table in expr_lower or table.split(".")[-1] in expr_lower:
            return True

    if source_table:
        source = source_table.lower()
        if source in expr_lower or source.split(".")[-1] in expr_lower:
            if widget_tables:
                source_bare = source.split(".")[-1]
                for wt in widget_tables:
                    if wt == source or wt == source_bare or wt.endswith(f".{source_bare}"):
                        return True

    # Unqualified column: check if bare column appears in widget SQL and schema overlap
    bare_col_match = re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", resolved_expression.strip())
    if bare_col_match and widget_tables:
        col = resolved_expression.lower()
        sql_lower = widget_sql.lower()
        if col in sql_lower:
            for table_key in widget_tables:
                for table in schema.get("tables") or []:
                    if not isinstance(table, dict):
                        continue
                    schema_name = str(table.get("schema_name") or "").lower()
                    bare_name = str(table.get("table_name") or "").lower()
                    qualified = f"{schema_name}.{bare_name}" if schema_name else bare_name
                    if table_key in {qualified, bare_name}:
                        for column in table.get("columns") or []:
                            if isinstance(column, dict) and str(column.get("name", "")).lower() == col:
                                return True
    return False


def filter_applies_to_widget(
    resolved_expression: str,
    widget_sql: str,
    schema: dict[str, Any],
    db_type: str,
    *,
    source_table: str | None = None,
) -> bool:
    return _expression_references_widget(
        resolved_expression,
        widget_sql,
        schema,
        db_type,
        source_table=source_table,
    )


def apply_filters_to_sql(base_sql: str, predicates: list[str], db_type: str) -> str:
    """Inject predicate SQL fragments into the outer SELECT via sqlglot."""
    if not predicates:
        return base_sql

    dialect = _sqlglot_dialect(db_type)
    statement = _parse_sql(base_sql, dialect)
    select = statement.find(exp.Select)
    if select is None:
        return base_sql

    new_conditions: list[exp.Expression] = []
    for predicate in predicates:
        parsed = sqlglot.parse_one(predicate, read=dialect)
        new_conditions.append(parsed)

    existing_where = select.args.get("where")
    if existing_where is not None and existing_where.this is not None:
        combined = existing_where.this
        for cond in new_conditions:
            combined = exp.and_(combined, cond)
        select.set("where", exp.Where(this=combined))
    elif new_conditions:
        combined = new_conditions[0]
        for cond in new_conditions[1:]:
            combined = exp.and_(combined, cond)
        select.set("where", exp.Where(this=combined))

    return statement.sql(dialect=dialect)


def apply_dashboard_filters(
    sql: str,
    *,
    filters: list[dict[str, Any]] | list[Any],
    semantic: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
    source_table: str | None = None,
) -> str:
    """Apply applicable dashboard filters to widget SQL."""
    if not filters or not sql.strip():
        return sql

    validated = validate_dashboard_filters(filters, semantic)
    if not validated:
        return sql

    all_predicates = build_filter_predicates(validated, semantic, db_type)
    applicable: list[str] = []
    for _field, resolved, predicate in all_predicates:
        if filter_applies_to_widget(
            resolved,
            sql,
            schema,
            db_type,
            source_table=source_table,
        ):
            applicable.append(predicate)

    return apply_filters_to_sql(sql, applicable, db_type)
