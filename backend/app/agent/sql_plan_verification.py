"""AST-based SQL ↔ QueryPlan consistency checks using sqlglot."""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.agent.query_plan import QueryPlan
from app.agent.query_plan_validation import PlanValidationResult, QueryClarification
from app.tools.sql_safety import _sqlglot_dialect


def _parse_select(sql: str, db_type: str) -> exp.Select | None:
    dialect = _sqlglot_dialect(db_type)
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(parsed, exp.Select):
        return parsed
    if isinstance(parsed, exp.Subquery) and isinstance(parsed.this, exp.Select):
        return parsed.this
    if isinstance(parsed, exp.With) and isinstance(parsed.this, exp.Select):
        return parsed.this
    return None


def _select_output_names(select: exp.Select) -> set[str]:
    names: set[str] = set()
    for proj in select.expressions:
        alias = proj.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _has_group_by(select: exp.Select) -> bool:
    return select.args.get("group") is not None


def _has_order_desc(select: exp.Select) -> bool:
    order = select.args.get("order")
    if not order:
        return False
    for ordered in order.expressions:
        if isinstance(ordered, exp.Ordered):
            if ordered.args.get("desc"):
                return True
    return False


def _has_limit(select: exp.Select) -> bool:
    return select.args.get("limit") is not None


def verify_sql_plan_ast(
    sql: str,
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    *,
    db_type: str = "postgres",
) -> PlanValidationResult:
    """Structural SQL verification via sqlglot when parsing succeeds."""
    if not sql:
        return PlanValidationResult(ok=True)

    select = _parse_select(sql, db_type)
    if select is None:
        return PlanValidationResult(ok=True)

    outputs = _select_output_names(select)

    from app.agent.sql_compiler import _catalog_maps

    metrics, dimensions = _catalog_maps(semantic_catalog)

    for metric in plan.metrics:
        alias = re.sub(r"[^a-zA-Z0-9_]+", "_", metric).strip("_").lower() or "metric"
        expr = (metrics.get(metric.lower()) or metric).lower()
        if alias not in outputs and not any(
            token in name for name in outputs for token in (alias, metric.lower())
        ):
            if not any(part in " ".join(outputs) for part in expr.split() if len(part) > 3):
                return PlanValidationResult(
                    ok=False,
                    errors=[f"AST: missing metric output for {metric}."],
                )

    if plan.dimensions and plan.intent in {"aggregation", "comparison", "share_of_total"}:
        if not _has_group_by(select) and plan.intent != "share_of_total":
            pass  # scalar comparison may omit GROUP BY
        for dim in plan.dimensions:
            alias = re.sub(r"[^a-zA-Z0-9_]+", "_", dim).strip("_").lower()
            if alias not in outputs and dim.lower() not in outputs:
                expr = (dimensions.get(dim.lower()) or dim).lower()
                if not any(part in " ".join(outputs) for part in expr.split(".") if part):
                    return PlanValidationResult(
                        ok=False,
                        errors=[f"AST: missing dimension {dim} in SELECT."],
                    )

    if plan.limit is not None and not _has_limit(select) and "top" not in sql.lower():
        return PlanValidationResult(
            ok=False,
            errors=["AST: missing LIMIT clause."],
            clarification=QueryClarification(
                kind="invalid_plan",
                message="The ranking limit could not be applied safely.",
            ),
        )

    if plan.sort and plan.sort[0].direction == "desc" and not _has_order_desc(select):
        return PlanValidationResult(ok=False, errors=["AST: missing DESC order."])

    if plan.intent == "comparison" and plan.comparison:
        if plan.comparison.type == "entity_vs_entity":
            if not any(n.startswith("entity_a_") or n.startswith("entity_b_") for n in outputs):
                return PlanValidationResult(ok=False, errors=["AST: missing entity comparison columns."])
        elif not any(n.startswith("current_") for n in outputs):
            return PlanValidationResult(ok=False, errors=["AST: missing current period column."])

    if plan.intent == "share_of_total":
        if "share_pct" not in outputs and not any("share" in n for n in outputs):
            return PlanValidationResult(ok=False, errors=["AST: missing share column."])

    return PlanValidationResult(ok=True)
