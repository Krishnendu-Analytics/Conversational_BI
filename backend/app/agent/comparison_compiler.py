"""Dual-period comparison, share-of-total, and entity comparison SQL compilation."""

from __future__ import annotations

import re
from typing import Any

from app.agent.join_planner import JoinAmbiguityError, build_join_clauses, tables_from_expression
from app.agent.query_plan import ComparisonSpec, FilterSpec, QueryPlan, TimeSpec
from app.agent.time_resolution import derive_comparison_period, primary_period_for_comparison_type
from app.agent.sql_compiler import (
    _catalog_maps,
    _date_bucket_expr,
    _filter_clause,
    _first_fact_table,
    _preceding_period_predicate,
    _quote_literal,
    _resolve_expression,
    _time_predicate,
)
from app.sql_dialect import compiler_db_type


def _metric_alias(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_") or "metric"


def _conditional_aggregate(metric_expr: str, predicate: str) -> str:
    upper = metric_expr.upper().strip()
    if upper.startswith("COUNT(DISTINCT "):
        inner = metric_expr[len("COUNT(DISTINCT ") : -1].strip()
        return f"COUNT(DISTINCT CASE WHEN {predicate} THEN {inner} END)"
    if upper.startswith("COUNT("):
        inner = metric_expr[len("COUNT(") : -1].strip()
        return f"COUNT(CASE WHEN {predicate} THEN {inner} END)"
    if upper.startswith("SUM("):
        inner = metric_expr[len("SUM(") : -1].strip()
        return f"SUM(CASE WHEN {predicate} THEN {inner} ELSE 0 END)"
    if upper.startswith("AVG("):
        inner = metric_expr[len("AVG(") : -1].strip()
        return f"AVG(CASE WHEN {predicate} THEN {inner} END)"
    if upper.startswith(("MIN(", "MAX(")):
        func = upper.split("(")[0]
        inner = metric_expr.split("(", 1)[1][:-1].strip()
        return f"{func}(CASE WHEN {predicate} THEN {inner} END)"
    return f"SUM(CASE WHEN {predicate} THEN {metric_expr} ELSE 0 END)"


def _resolve_period_pair(plan: QueryPlan) -> tuple[TimeSpec, TimeSpec]:
    comp = plan.comparison
    if comp is None:
        raise ValueError("Comparison plan requires comparison spec")

    primary = plan.time or TimeSpec(range="ytd")
    if comp.comparison_time is not None:
        return primary, comp.comparison_time

    if comp.type == "previous_year":
        return (
            TimeSpec(field=primary.field, grain=primary.grain, range="ytd"),
            TimeSpec(field=primary.field, grain=primary.grain, range="last_year"),
        )
    if comp.type == "previous_month":
        return (
            TimeSpec(field=primary.field, grain=primary.grain, range="this_month"),
            TimeSpec(field=primary.field, grain=primary.grain, range="last_month"),
        )
    if comp.type == "previous_quarter":
        return (
            TimeSpec(field=primary.field, grain=primary.grain, range="this_quarter"),
            TimeSpec(field=primary.field, grain=primary.grain, range="last_quarter"),
        )
    if comp.type == "previous_period":
        derived = derive_comparison_period(primary)
        if derived is None:
            primary = primary_period_for_comparison_type("previous_period", primary.field)
            derived = derive_comparison_period(primary)
        if derived is None:
            raise ValueError("Cannot derive previous period from query context")
        return primary, derived

    raise ValueError(f"Unsupported comparison type: {comp.type}")


def _build_from_clause(
    required_tables: set[str],
    schema: dict[str, Any],
    relationships: list[Any],
) -> str:
    if not required_tables:
        from_table = _first_fact_table(schema)
        if from_table is None:
            raise ValueError("Schema has no tables")
        return from_table
    if len(required_tables) == 1:
        return next(iter(required_tables))
    from_clause, _ = build_join_clauses(
        required_tables,
        schema,
        relationships,
        base_table=sorted(required_tables)[0],
    )
    return from_clause


def _resolve_date_expr(plan: QueryPlan, metrics: dict[str, str], dimensions: dict[str, str]) -> str | None:
    if plan.time and plan.time.field:
        return _resolve_expression(plan.time.field, metrics, dimensions) or plan.time.field
    for candidate in plan.dimensions:
        resolved = _resolve_expression(candidate, metrics, dimensions)
        if resolved and any(token in resolved.lower() for token in ("date", "_at", "time")):
            return resolved
    return None


def compile_dual_period_comparison(
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
) -> str:
    """Compile a single grouped query with current and comparison period metrics."""
    if plan.comparison is None:
        raise ValueError("Dual-period compilation requires comparison spec")

    db_type = compiler_db_type(db_type)
    metrics_map, dimensions_map = _catalog_maps(semantic_catalog)
    relationships = (semantic_catalog or {}).get("relationships") or []
    primary_spec, comparison_spec = _resolve_period_pair(plan)
    date_expr = _resolve_date_expr(plan, metrics_map, dimensions_map)
    if not date_expr:
        raise ValueError("Comparison queries require a date field")

    primary_pred = _time_predicate(primary_spec, date_expr, db_type)
    if comparison_spec.range == "previous_period":
        comparison_pred = _preceding_period_predicate(primary_spec, date_expr, db_type)
    else:
        comparison_pred = _time_predicate(comparison_spec, date_expr, db_type)
    if not primary_pred or not comparison_pred:
        raise ValueError("Could not compile time predicates for comparison periods")

    required_tables: set[str] = set()
    select_parts: list[str] = []
    group_parts: list[str] = []

    for dim in plan.dimensions:
        expr = _resolve_expression(dim, metrics_map, dimensions_map) or dim
        required_tables.update(tables_from_expression(expr, schema))
        alias = _metric_alias(dim)
        select_parts.append(f"{expr} AS {alias}")
        group_parts.append(expr)

    if plan.time and plan.time.grain and plan.dimensions:
        bucket = _date_bucket_expr(date_expr, plan.time.grain, db_type)
        if bucket not in group_parts:
            select_parts.insert(0, f"{bucket} AS period")
            group_parts.insert(0, bucket)

    metric_exprs: list[tuple[str, str]] = []
    for metric in plan.metrics:
        expr = _resolve_expression(metric, metrics_map, dimensions_map) or metric
        required_tables.update(tables_from_expression(expr, schema))
        if not expr.upper().startswith(("SUM(", "COUNT(", "AVG(", "MIN(", "MAX(")):
            expr = f"SUM({expr})"
        metric_exprs.append((metric, expr))

    required_tables.update(tables_from_expression(date_expr, schema))
    for filt in plan.filters:
        fexpr = _resolve_expression(filt.field, metrics_map, dimensions_map) or filt.field
        required_tables.update(tables_from_expression(fexpr, schema))

    from_clause = _build_from_clause(required_tables, schema, relationships)

    for metric_name, expr in metric_exprs:
        alias = _metric_alias(metric_name)
        current_alias = f"current_{alias}"
        comparison_alias = f"comparison_{alias}"
        select_parts.append(f"{_conditional_aggregate(expr, primary_pred)} AS {current_alias}")
        select_parts.append(f"{_conditional_aggregate(expr, comparison_pred)} AS {comparison_alias}")
        if plan.comparison.include_absolute_change:
            select_parts.append(
                f"({_conditional_aggregate(expr, primary_pred)} - "
                f"{_conditional_aggregate(expr, comparison_pred)}) AS absolute_change_{alias}"
            )
        if plan.comparison.include_percent_change:
            select_parts.append(
                f"CASE WHEN {_conditional_aggregate(expr, comparison_pred)} = 0 THEN NULL "
                f"ELSE (({_conditional_aggregate(expr, primary_pred)} - "
                f"{_conditional_aggregate(expr, comparison_pred)}) / "
                f"NULLIF({_conditional_aggregate(expr, comparison_pred)}, 0)) * 100 END "
                f"AS percent_change_{alias}"
            )

    where_parts = [
        f"({primary_pred} OR {comparison_pred})",
    ]
    for filt in plan.filters:
        fexpr = _resolve_expression(filt.field, metrics_map, dimensions_map) or filt.field
        where_parts.append(_filter_clause(filt, fexpr, db_type))

    sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"
    sql += " WHERE " + " AND ".join(where_parts)
    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)

    if plan.sort:
        order_bits = []
        for item in plan.sort:
            sort_expr = _resolve_expression(item.field, metrics_map, dimensions_map) or item.field
            if sort_expr.lower() in {m.lower() for m in plan.metrics}:
                sort_expr = f"current_{_metric_alias(item.field)}"
            order_bits.append(f"{sort_expr} {item.direction.upper()}")
        sql += " ORDER BY " + ", ".join(order_bits)
    elif metric_exprs:
        sql += f" ORDER BY current_{_metric_alias(metric_exprs[0][0])} DESC"

    if plan.limit is not None:
        db_key = (db_type or "").lower()
        if db_key == "mssql":
            sql = sql.replace("SELECT ", f"SELECT TOP {int(plan.limit)} ", 1)
        else:
            sql += f" LIMIT {int(plan.limit)}"
    return sql


def compile_share_of_total(
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
) -> str:
    """Compile grouped share-of-total using window aggregation over filtered scope."""
    db_type = compiler_db_type(db_type)
    metrics_map, dimensions_map = _catalog_maps(semantic_catalog)
    relationships = (semantic_catalog or {}).get("relationships") or []

    select_parts: list[str] = []
    group_parts: list[str] = []
    required_tables: set[str] = set()

    for dim in plan.dimensions:
        expr = _resolve_expression(dim, metrics_map, dimensions_map) or dim
        required_tables.update(tables_from_expression(expr, schema))
        alias = _metric_alias(dim)
        select_parts.append(f"{expr} AS {alias}")
        group_parts.append(expr)

    if not plan.metrics:
        raise ValueError("Share-of-total requires a metric")
    metric_name = plan.metrics[0]
    expr = _resolve_expression(metric_name, metrics_map, dimensions_map) or metric_name
    required_tables.update(tables_from_expression(expr, schema))
    if not expr.upper().startswith(("SUM(", "COUNT(", "AVG(")):
        expr = f"SUM({expr})"
    alias = _metric_alias(metric_name)
    value_alias = alias
    select_parts.append(f"{expr} AS {value_alias}")

    date_expr = _resolve_date_expr(plan, metrics_map, dimensions_map)
    if date_expr:
        required_tables.update(tables_from_expression(date_expr, schema))

    for filt in plan.filters:
        fexpr = _resolve_expression(filt.field, metrics_map, dimensions_map) or filt.field
        required_tables.update(tables_from_expression(fexpr, schema))

    from_clause = _build_from_clause(required_tables, schema, relationships)

    where_parts: list[str] = []
    for filt in plan.filters:
        fexpr = _resolve_expression(filt.field, metrics_map, dimensions_map) or filt.field
        where_parts.append(_filter_clause(filt, fexpr, db_type))
    if plan.time and date_expr:
        predicate = _time_predicate(plan.time, date_expr, db_type)
        if predicate:
            where_parts.append(predicate)

    inner = f"SELECT {', '.join(select_parts)} FROM {from_clause}"
    if where_parts:
        inner += " WHERE " + " AND ".join(where_parts)
    if group_parts:
        inner += " GROUP BY " + ", ".join(group_parts)

    dim_cols = ", ".join(
        _metric_alias(d) for d in plan.dimensions
    ) if plan.dimensions else ""
    outer_select = dim_cols + (", " if dim_cols else "") + f"{value_alias}, "
    outer_select += (
        f"CASE WHEN SUM({value_alias}) OVER () = 0 THEN NULL "
        f"ELSE ({value_alias} * 100.0 / SUM({value_alias}) OVER ()) END AS share_pct"
    )
    sql = f"SELECT {outer_select} FROM ({inner}) AS share_base"
    if plan.sort:
        primary = plan.sort[0]
        col = _metric_alias(primary.field) if primary.field in plan.dimensions else value_alias
        sql += f" ORDER BY {col} {primary.direction.upper()}"
    elif plan.dimensions:
        sql += f" ORDER BY share_pct DESC"
    if plan.limit is not None:
        db_key = (db_type or "").lower()
        if db_key == "mssql":
            sql = sql.replace("SELECT ", f"SELECT TOP {int(plan.limit)} ", 1)
        else:
            sql += f" LIMIT {int(plan.limit)}"
    return sql


def compile_entity_vs_entity(
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
) -> str:
    """Compile entity-vs-entity comparison as side-by-side metric columns."""
    db_type = compiler_db_type(db_type)
    comp = plan.comparison
    if comp is None or comp.type != "entity_vs_entity":
        raise ValueError("Entity comparison requires comparison spec")
    if not comp.entities or len(comp.entities) < 2:
        raise ValueError("Entity comparison requires at least two entities")
    if not comp.entity_dimension:
        raise ValueError("Entity comparison requires entity_dimension")

    metrics_map, dimensions_map = _catalog_maps(semantic_catalog)
    relationships = (semantic_catalog or {}).get("relationships") or []
    dim_expr = _resolve_expression(comp.entity_dimension, metrics_map, dimensions_map)
    if not dim_expr:
        raise ValueError(f"Unknown entity dimension: {comp.entity_dimension}")

    required_tables: set[str] = set()
    required_tables.update(tables_from_expression(dim_expr, schema))

    select_parts: list[str] = []
    entity_a, entity_b = comp.entities[0], comp.entities[1]
    safe_a = _quote_literal(entity_a, db_type)
    safe_b = _quote_literal(entity_b, db_type)

    if not plan.metrics:
        raise ValueError("Entity comparison requires a metric")
    metric_name = plan.metrics[0]
    expr = _resolve_expression(metric_name, metrics_map, dimensions_map) or metric_name
    required_tables.update(tables_from_expression(expr, schema))
    if not expr.upper().startswith(("SUM(", "COUNT(", "AVG(")):
        expr = f"SUM({expr})"

    alias = _metric_alias(metric_name)
    select_parts.append(
        f"{_conditional_aggregate(expr, f'{dim_expr} = {safe_a}')} AS entity_a_{alias}"
    )
    select_parts.append(
        f"{_conditional_aggregate(expr, f'{dim_expr} = {safe_b}')} AS entity_b_{alias}"
    )
    if comp.include_absolute_change:
        select_parts.append(
            f"({_conditional_aggregate(expr, f'{dim_expr} = {safe_a}')} - "
            f"{_conditional_aggregate(expr, f'{dim_expr} = {safe_b}')}) AS absolute_change_{alias}"
        )
    if comp.include_percent_change:
        select_parts.append(
            f"CASE WHEN {_conditional_aggregate(expr, f'{dim_expr} = {safe_b}')} = 0 THEN NULL "
            f"ELSE (({_conditional_aggregate(expr, f'{dim_expr} = {safe_a}')} - "
            f"{_conditional_aggregate(expr, f'{dim_expr} = {safe_b}')}) / "
            f"NULLIF({_conditional_aggregate(expr, f'{dim_expr} = {safe_b}')}, 0)) * 100 END "
            f"AS percent_change_{alias}"
        )

    date_expr = _resolve_date_expr(plan, metrics_map, dimensions_map)
    if date_expr:
        required_tables.update(tables_from_expression(date_expr, schema))

    for filt in plan.filters:
        fexpr = _resolve_expression(filt.field, metrics_map, dimensions_map) or filt.field
        required_tables.update(tables_from_expression(fexpr, schema))

    from_clause = _build_from_clause(required_tables, schema, relationships)
    where_parts = [f"{dim_expr} IN ({safe_a}, {safe_b})"]
    for filt in plan.filters:
        if filt.field.lower() == comp.entity_dimension.lower():
            continue
        fexpr = _resolve_expression(filt.field, metrics_map, dimensions_map) or filt.field
        where_parts.append(_filter_clause(filt, fexpr, db_type))
    if plan.time and date_expr:
        predicate = _time_predicate(plan.time, date_expr, db_type)
        if predicate:
            where_parts.append(predicate)

    sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"
    sql += " WHERE " + " AND ".join(where_parts)
    return sql


def needs_specialized_compile(plan: QueryPlan) -> bool:
    if plan.intent == "share_of_total":
        return True
    if plan.comparison is None:
        return False
    return plan.comparison.type in {
        "previous_period",
        "previous_year",
        "previous_month",
        "previous_quarter",
        "entity_vs_entity",
        "share_of_total",
    } or plan.intent == "comparison"


def compile_specialized_plan(
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
) -> str:
    if plan.intent == "share_of_total" or (
        plan.comparison and plan.comparison.type == "share_of_total"
    ):
        return compile_share_of_total(plan, semantic_catalog, schema, db_type)
    if plan.comparison and plan.comparison.type == "entity_vs_entity":
        return compile_entity_vs_entity(plan, semantic_catalog, schema, db_type)
    if plan.intent == "comparison" or (
        plan.comparison and plan.comparison.type in {
            "previous_period",
            "previous_year",
            "previous_month",
            "previous_quarter",
        }
    ):
        return compile_dual_period_comparison(plan, semantic_catalog, schema, db_type)
    raise ValueError("Plan does not require specialized comparison compilation")
