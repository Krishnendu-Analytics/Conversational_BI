"""Compile structured query plans into deterministic SQL."""

from __future__ import annotations

import re
from typing import Any

from app.agent.join_planner import JoinAmbiguityError, build_join_clauses, tables_from_expression
from app.agent.query_plan import FilterSpec, QueryPlan, TimeSpec
from app.services.semantic_identity import expression_map_for_items


def _postgres_time_mapping(date_expr: str) -> dict[str, str]:
    return {
        "today": f"{date_expr} = CURRENT_DATE",
        "yesterday": f"{date_expr} = CURRENT_DATE - INTERVAL '1 day'",
        "this_week": f"{date_expr} >= DATE_TRUNC('week', CURRENT_DATE)",
        "last_week": (
            f"{date_expr} >= DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '1 week' "
            f"AND {date_expr} < DATE_TRUNC('week', CURRENT_DATE)"
        ),
        "this_month": f"{date_expr} >= DATE_TRUNC('month', CURRENT_DATE)",
        "mtd": f"{date_expr} >= DATE_TRUNC('month', CURRENT_DATE) AND {date_expr} <= CURRENT_DATE",
        "last_month": (
            f"{date_expr} >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month' "
            f"AND {date_expr} < DATE_TRUNC('month', CURRENT_DATE)"
        ),
        "this_quarter": f"{date_expr} >= DATE_TRUNC('quarter', CURRENT_DATE)",
        "last_quarter": (
            f"{date_expr} >= DATE_TRUNC('quarter', CURRENT_DATE) - INTERVAL '3 months' "
            f"AND {date_expr} < DATE_TRUNC('quarter', CURRENT_DATE)"
        ),
        "this_year": f"{date_expr} >= DATE_TRUNC('year', CURRENT_DATE)",
        "ytd": f"{date_expr} >= DATE_TRUNC('year', CURRENT_DATE) AND {date_expr} <= CURRENT_DATE",
        "last_year": (
            f"{date_expr} >= DATE_TRUNC('year', CURRENT_DATE) - INTERVAL '1 year' "
            f"AND {date_expr} < DATE_TRUNC('year', CURRENT_DATE)"
        ),
        "last_7_days": f"{date_expr} >= CURRENT_DATE - INTERVAL '7 days'",
        "last_30_days": f"{date_expr} >= CURRENT_DATE - INTERVAL '30 days'",
        "previous_period": (
            f"{date_expr} >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month' "
            f"AND {date_expr} < DATE_TRUNC('month', CURRENT_DATE)"
        ),
    }


def _preceding_period_predicate(primary: TimeSpec, date_expr: str, db_type: str) -> str | None:
    """SQL predicate for the period immediately before the primary period."""
    key = (db_type or "").lower()
    pr = primary.range
    if key == "postgres":
        if pr == "last_7_days":
            return (
                f"{date_expr} >= CURRENT_DATE - INTERVAL '14 days' "
                f"AND {date_expr} < CURRENT_DATE - INTERVAL '7 days'"
            )
        if pr == "last_30_days":
            return (
                f"{date_expr} >= CURRENT_DATE - INTERVAL '60 days' "
                f"AND {date_expr} < CURRENT_DATE - INTERVAL '30 days'"
            )
        if pr == "last_week":
            return (
                f"{date_expr} >= DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '2 weeks' "
                f"AND {date_expr} < DATE_TRUNC('week', CURRENT_DATE) - INTERVAL '1 week'"
            )
        if pr == "last_month":
            return (
                f"{date_expr} >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '2 months' "
                f"AND {date_expr} < DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'"
            )
        if pr == "last_quarter":
            return (
                f"{date_expr} >= DATE_TRUNC('quarter', CURRENT_DATE) - INTERVAL '6 months' "
                f"AND {date_expr} < DATE_TRUNC('quarter', CURRENT_DATE) - INTERVAL '3 months'"
            )
        if pr == "last_year":
            return (
                f"{date_expr} >= DATE_TRUNC('year', CURRENT_DATE) - INTERVAL '2 years' "
                f"AND {date_expr} < DATE_TRUNC('year', CURRENT_DATE) - INTERVAL '1 year'"
            )
    return _time_predicate(TimeSpec(range="last_month"), date_expr, db_type)


def _catalog_maps(semantic_catalog: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    if not semantic_catalog:
        return {}, {}
    return (
        expression_map_for_items(semantic_catalog.get("metrics")),
        expression_map_for_items(semantic_catalog.get("dimensions")),
    )


def _first_fact_table(schema: dict[str, Any]) -> str | None:
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("table_name")
        if not name:
            continue
        schema_name = table.get("schema_name") or ""
        return f"{schema_name}.{name}" if schema_name else str(name)
    return None


def _resolve_expression(name: str, metrics: dict[str, str], dimensions: dict[str, str]) -> str | None:
    key = name.lower()
    if key in metrics:
        return metrics[key]
    if key in dimensions:
        return dimensions[key]
    return None


def _quote_literal(value: Any, db_type: str) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _filter_clause(spec: FilterSpec, expr: str, db_type: str) -> str:
    op = spec.operator
    value = spec.value
    if op == "eq":
        return f"{expr} = {_quote_literal(value, db_type)}"
    if op == "neq":
        return f"{expr} <> {_quote_literal(value, db_type)}"
    if op == "gt":
        return f"{expr} > {_quote_literal(value, db_type)}"
    if op == "gte":
        return f"{expr} >= {_quote_literal(value, db_type)}"
    if op == "lt":
        return f"{expr} < {_quote_literal(value, db_type)}"
    if op == "lte":
        return f"{expr} <= {_quote_literal(value, db_type)}"
    if op == "like":
        return f"{expr} LIKE {_quote_literal(value, db_type)}"
    if op == "in" and isinstance(value, list):
        values = ", ".join(_quote_literal(v, db_type) for v in value)
        return f"{expr} IN ({values})"
    return f"{expr} = {_quote_literal(value, db_type)}"


def _mysql_current_quarter_start() -> str:
    """First day of the current calendar quarter (sqlglot-parseable MySQL)."""
    return (
        "DATE(CONCAT(YEAR(CURDATE()), '-', "
        "LPAD(QUARTER(CURDATE()) * 3 - 2, 2, '0'), '-01'))"
    )


def _time_predicate(time_spec: TimeSpec, date_expr: str, db_type: str) -> str | None:
    if time_spec is None:
        return None
    key = (db_type or "").lower()
    if time_spec.start and time_spec.end:
        return (
            f"{date_expr} >= {_quote_literal(time_spec.start, db_type)} "
            f"AND {date_expr} <= {_quote_literal(time_spec.end, db_type)}"
        )
    range_key = time_spec.range
    if range_key == "all" or range_key is None:
        return None
    if key == "postgres":
        mapping = _postgres_time_mapping(date_expr)
        pred = mapping.get(range_key or "")
        if pred:
            return pred
    if key == "mysql":
        mapping = {
            "last_7_days": f"{date_expr} >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)",
            "last_30_days": f"{date_expr} >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)",
            "last_month": (
                f"{date_expr} >= DATE_SUB(DATE_SUB(CURDATE(), INTERVAL DAY(CURDATE())-1 DAY), INTERVAL 1 MONTH) "
                f"AND {date_expr} < DATE_SUB(CURDATE(), INTERVAL DAY(CURDATE())-1 DAY)"
            ),
            "this_month": f"{date_expr} >= DATE_SUB(CURDATE(), INTERVAL DAY(CURDATE())-1 DAY)",
            "last_quarter": (
                f"{date_expr} >= DATE_SUB({_mysql_current_quarter_start()}, INTERVAL 3 MONTH) "
                f"AND {date_expr} < {_mysql_current_quarter_start()}"
            ),
            "last_year": (
                f"{date_expr} >= DATE_SUB(MAKEDATE(YEAR(CURDATE())-1, 1), INTERVAL 0 DAY) "
                f"AND {date_expr} < MAKEDATE(YEAR(CURDATE()), 1)"
            ),
            "this_year": f"{date_expr} >= MAKEDATE(YEAR(CURDATE()), 1)",
            "ytd": f"{date_expr} >= MAKEDATE(YEAR(CURDATE()), 1) AND {date_expr} <= CURDATE()",
            "mtd": (
                f"{date_expr} >= DATE_SUB(CURDATE(), INTERVAL DAY(CURDATE())-1 DAY) "
                f"AND {date_expr} <= CURDATE()"
            ),
            "today": f"DATE({date_expr}) = CURDATE()",
            "yesterday": f"DATE({date_expr}) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)",
            "this_week": f"{date_expr} >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)",
            "last_week": (
                f"{date_expr} >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE())+7 DAY) "
                f"AND {date_expr} < DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)"
            ),
            "this_quarter": (
                f"{date_expr} >= MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE())-1) QUARTER"
            ),
        }
        return mapping.get(range_key)
    if key == "mssql":
        mapping = {
            "last_7_days": f"{date_expr} >= DATEADD(day, -7, CAST(GETDATE() AS date))",
            "last_30_days": f"{date_expr} >= DATEADD(day, -30, CAST(GETDATE() AS date))",
            "last_month": (
                f"{date_expr} >= DATEADD(month, DATEDIFF(month, 0, GETDATE()) - 1, 0) "
                f"AND {date_expr} < DATEADD(month, DATEDIFF(month, 0, GETDATE()), 0)"
            ),
            "this_month": f"{date_expr} >= DATEADD(month, DATEDIFF(month, 0, GETDATE()), 0)",
            "last_quarter": (
                f"{date_expr} >= DATEADD(quarter, DATEDIFF(quarter, 0, GETDATE()) - 1, 0) "
                f"AND {date_expr} < DATEADD(quarter, DATEDIFF(quarter, 0, GETDATE()), 0)"
            ),
            "last_year": (
                f"{date_expr} >= DATEFROMPARTS(YEAR(GETDATE()) - 1, 1, 1) "
                f"AND {date_expr} < DATEFROMPARTS(YEAR(GETDATE()), 1, 1)"
            ),
            "this_year": f"{date_expr} >= DATEFROMPARTS(YEAR(GETDATE()), 1, 1)",
            "ytd": (
                f"{date_expr} >= DATEFROMPARTS(YEAR(GETDATE()), 1, 1) "
                f"AND {date_expr} <= CAST(GETDATE() AS date)"
            ),
            "mtd": (
                f"{date_expr} >= DATEADD(month, DATEDIFF(month, 0, GETDATE()), 0) "
                f"AND {date_expr} <= CAST(GETDATE() AS date)"
            ),
            "today": f"CAST({date_expr} AS date) = CAST(GETDATE() AS date)",
            "yesterday": f"CAST({date_expr} AS date) = DATEADD(day, -1, CAST(GETDATE() AS date))",
            "this_week": f"{date_expr} >= DATEADD(week, DATEDIFF(week, 0, GETDATE()), 0)",
            "last_week": (
                f"{date_expr} >= DATEADD(week, DATEDIFF(week, 0, GETDATE()) - 1, 0) "
                f"AND {date_expr} < DATEADD(week, DATEDIFF(week, 0, GETDATE()), 0)"
            ),
            "this_quarter": f"{date_expr} >= DATEADD(quarter, DATEDIFF(quarter, 0, GETDATE()), 0)",
        }
        return mapping.get(range_key)
    return None


def _date_bucket_expr(expr: str, grain: str | None, db_type: str) -> str:
    grain = grain or "month"
    key = (db_type or "").lower()
    if key == "postgres":
        unit = {"day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year"}.get(
            grain, "month"
        )
        return f"DATE_TRUNC('{unit}', {expr})"
    if key == "mysql":
        if grain == "day":
            return f"DATE({expr})"
        if grain == "week":
            return f"DATE_SUB({expr}, INTERVAL WEEKDAY({expr}) DAY)"
        if grain == "year":
            return f"DATE_FORMAT({expr}, '%Y-01-01')"
        if grain == "quarter":
            return (
                f"MAKEDATE(YEAR({expr}), 1) + INTERVAL (QUARTER({expr}) - 1) QUARTER"
            )
        return f"DATE_FORMAT({expr}, '%Y-%m-01')"
    if key == "mssql":
        if grain == "day":
            return f"CAST({expr} AS date)"
        if grain == "year":
            return f"DATEFROMPARTS(YEAR({expr}), 1, 1)"
        if grain == "quarter":
            return f"DATEADD(quarter, DATEDIFF(quarter, 0, {expr}), 0)"
        if grain == "week":
            return f"DATEADD(week, DATEDIFF(week, 0, {expr}), 0)"
        return f"DATEADD(month, DATEDIFF(month, 0, {expr}), 0)"
    return expr


def _needs_default_sum(expr: str) -> bool:
    """Wrap bare columns in SUM; leave aggregates and ratio expressions alone."""
    compact = re.sub(r"\s+", "", (expr or "").upper())
    if compact.startswith(("SUM(", "COUNT(", "AVG(", "MIN(", "MAX(", "(")):
        return False
    # Any aggregate anywhere ("100.0 * AVG(...)") means the expression is
    # already aggregated — wrapping it again would nest aggregates.
    if re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\(", compact):
        return False
    if "NULLIF(" in compact or "/" in (expr or ""):
        return False
    return True


def compile_query_plan(
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    schema: dict[str, Any],
    db_type: str,
) -> str:
    """Build a deterministic SELECT from a query plan and semantic catalog."""
    from app.agent.comparison_compiler import compile_specialized_plan, needs_specialized_compile
    from app.sql_dialect import compiler_db_type

    db_type = compiler_db_type(db_type)

    if needs_specialized_compile(plan):
        return compile_specialized_plan(plan, semantic_catalog, schema, db_type)

    metrics, dimensions = _catalog_maps(semantic_catalog)
    relationships = (semantic_catalog or {}).get("relationships") or []

    select_parts: list[str] = []
    group_parts: list[str] = []
    required_tables: set[str] = set()

    for dim in plan.dimensions:
        expr = _resolve_expression(dim, metrics, dimensions) or dim
        required_tables.update(tables_from_expression(expr, schema))
        alias = re.sub(r"[^a-zA-Z0-9_]+", "_", dim).strip("_") or "dimension"
        select_parts.append(f"{expr} AS {alias}")
        group_parts.append(expr)

    for metric in plan.metrics:
        expr = _resolve_expression(metric, metrics, dimensions) or metric
        required_tables.update(tables_from_expression(expr, schema))
        alias = re.sub(r"[^a-zA-Z0-9_]+", "_", metric).strip("_") or "metric"
        if plan.intent in {"aggregation", "comparison", "share_of_total"} and _needs_default_sum(expr):
            expr = f"SUM({expr})"
        select_parts.append(f"{expr} AS {alias}")

    for filt in plan.filters:
        expr = _resolve_expression(filt.field, metrics, dimensions) or filt.field
        required_tables.update(tables_from_expression(expr, schema))

    date_expr: str | None = None
    if plan.time and plan.time.field:
        date_expr = _resolve_expression(plan.time.field, metrics, dimensions) or plan.time.field
        required_tables.update(tables_from_expression(date_expr, schema))
    elif plan.time:
        for candidate in plan.dimensions:
            resolved = _resolve_expression(candidate, metrics, dimensions)
            if resolved and any(token in resolved.lower() for token in ("date", "_at", "time")):
                date_expr = resolved
                required_tables.update(tables_from_expression(resolved, schema))
                break

    if not required_tables:
        from_table = _first_fact_table(schema)
        if from_table is None:
            raise ValueError("Schema has no tables")
        from_clause = from_table
    elif len(required_tables) == 1:
        from_clause = next(iter(required_tables))
    else:
        from_clause, _ = build_join_clauses(
            required_tables,
            schema,
            relationships,
            base_table=sorted(required_tables)[0],
        )

    if not select_parts:
        if plan.intent == "detail":
            select_parts.append("*")
        else:
            select_parts.append("COUNT(*) AS row_count")

    where_parts: list[str] = []
    for filt in plan.filters:
        expr = _resolve_expression(filt.field, metrics, dimensions) or filt.field
        where_parts.append(_filter_clause(filt, expr, db_type))

    if plan.time and date_expr:
        if plan.time.grain and plan.intent in {"aggregation", "comparison", "share_of_total"}:
            bucket = _date_bucket_expr(date_expr, plan.time.grain, db_type)
            if bucket not in group_parts:
                select_parts.insert(0, f"{bucket} AS period")
                group_parts.insert(0, bucket)
        predicate = _time_predicate(plan.time, date_expr, db_type)
        if predicate:
            where_parts.append(predicate)

    sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)
    if group_parts and plan.intent in {"aggregation", "comparison", "share_of_total"}:
        sql += " GROUP BY " + ", ".join(group_parts)
    if plan.sort:
        order_bits = []
        for item in plan.sort:
            expr = _resolve_expression(item.field, metrics, dimensions) or item.field
            order_bits.append(f"{expr} {item.direction.upper()}")
        sql += " ORDER BY " + ", ".join(order_bits)
    elif plan.intent in {"aggregation", "comparison", "share_of_total"} and select_parts and not plan.sort:
        # MySQL/Postgres reject ORDER BY 2 when SELECT has a single column.
        ordinal = 2 if len(select_parts) >= 2 else 1
        sql += f" ORDER BY {ordinal} DESC"

    limit = plan.limit if plan.limit is not None else (100 if plan.intent == "detail" else None)
    db_key = (db_type or "").lower()
    if limit is not None:
        if db_key == "mssql":
            sql = sql.replace("SELECT ", f"SELECT TOP {int(limit)} ", 1)
        else:
            sql += f" LIMIT {int(limit)}"
    return sql


# Re-export for callers/tests
__all__ = ["compile_query_plan", "JoinAmbiguityError"]
