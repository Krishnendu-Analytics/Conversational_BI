"""Deterministic validation for structured analytical query plans."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.query_plan import ComparisonSpec, FilterSpec, QueryPlan, SortSpec, TimeSpec
from app.agent.time_resolution import (
    derive_comparison_period,
    infer_comparison_type,
    primary_period_for_comparison_type,
    resolve_time_phrase,
)
from app.services.semantic_identity import (
    ambiguous_alias_targets,
    canonical_name_map,
    match_catalog_names,
)


class QueryClarificationChoice(BaseModel):
    id: str
    label: str


class QueryClarification(BaseModel):
    kind: Literal[
        "pick_metric",
        "pick_dimension",
        "pick_date_field",
        "join_path",
        "invalid_plan",
    ]
    message: str
    choices: list[QueryClarificationChoice] = Field(default_factory=list)


class PlanValidationResult(BaseModel):
    ok: bool = True
    clarification: QueryClarification | None = None
    errors: list[str] = Field(default_factory=list)


def _catalog_names(catalog: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str], list[str]]:
    metrics: dict[str, str] = {}
    dimensions: dict[str, str] = {}
    date_dimensions: list[str] = []
    if not catalog:
        return metrics, dimensions, date_dimensions

    metrics = canonical_name_map(catalog.get("metrics"))
    dimensions = canonical_name_map(catalog.get("dimensions"))
    for item in catalog.get("dimensions") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        expr = str(item.get("expression") or name).lower()
        fmt = str(item.get("format") or "").lower()
        if fmt == "date" or any(token in expr for token in ("date", "_at", "time", "timestamp")):
            date_dimensions.append(name)

    return metrics, dimensions, date_dimensions


def _question_metric_candidates(question: str, catalog: dict[str, Any] | None) -> list[str]:
    if not catalog:
        return []
    return match_catalog_names(question, catalog.get("metrics"))


def _normalize_plan_sort_for_ranking(plan: QueryPlan) -> QueryPlan:
    """Ensure top/bottom-N requests sort on the primary metric."""
    if plan.limit is None or not plan.metrics:
        return plan
    updated = plan.model_copy(deep=True)
    metric = updated.metrics[0]
    text = " ".join(updated.metrics).lower()
    direction: Literal["asc", "desc"] = "desc"
    if any(token in text for token in ("bottom", "lowest", "least")):
        direction = "asc"
    updated.sort = [SortSpec(field=metric, direction=direction)]
    return updated


def _extract_entity_pair(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    time_tokens = (
        "last year",
        "this year",
        "last month",
        "this month",
        "last quarter",
        "this quarter",
        "previous period",
        "year to date",
        "ytd",
    )
    if any(token in lowered for token in time_tokens):
        return None
    patterns = [
        r"compare\s+(.+?)\s+(?:with|vs|versus)\s+(.+)",
        r"(.+?)\s+(?:vs|versus)\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text.strip(), flags=re.IGNORECASE)
        if match:
            left = match.group(1).strip().strip("\"'")
            right = match.group(2).strip().strip("\"'")
            if left and right and len(left) < 80 and len(right) < 80:
                return left, right
    return None


def normalize_comparison_plan(
    plan: QueryPlan,
    *,
    question: str = "",
) -> QueryPlan:
    """Fill comparison semantics from NL when intent is comparison or share_of_total."""
    plan = plan.model_copy(deep=True)
    comp_type = infer_comparison_type(question, plan.time)

    if plan.intent == "share_of_total" or comp_type == "share_of_total":
        plan.intent = "share_of_total"
        if plan.comparison is None:
            plan.comparison = ComparisonSpec(type="share_of_total")
        return plan

    if plan.intent == "comparison" or comp_type:
        plan.intent = "comparison"
        if plan.comparison is None and comp_type:
            plan.comparison = ComparisonSpec(type=comp_type)  # type: ignore[arg-type]
        if plan.comparison is None:
            plan.comparison = ComparisonSpec(type="previous_year")
        if plan.time is None and plan.comparison.type in {
            "previous_year",
            "previous_month",
            "previous_quarter",
            "previous_period",
        }:
            plan.time = primary_period_for_comparison_type(plan.comparison.type)
        if plan.comparison.type == "previous_period" and plan.time:
            derived = derive_comparison_period(plan.time)
            if derived:
                plan.comparison = plan.comparison.model_copy(update={"comparison_time": derived})

    entity_pair = _extract_entity_pair(question)
    if entity_pair and not comp_type:
        plan.intent = "comparison"
        entity_dim = plan.comparison.entity_dimension if plan.comparison else None
        if not entity_dim and plan.dimensions:
            entity_dim = plan.dimensions[0]
        plan.comparison = ComparisonSpec(
            type="entity_vs_entity",
            entity_dimension=entity_dim,
            entities=[entity_pair[0], entity_pair[1]],
        )

    if plan.comparison and plan.comparison.type == "entity_vs_entity":
        if not plan.comparison.entity_dimension and plan.dimensions:
            plan.comparison = plan.comparison.model_copy(
                update={"entity_dimension": plan.dimensions[0]}
            )

    return plan


def normalize_query_plan(
    plan: QueryPlan,
    *,
    question: str = "",
    semantic_catalog: dict[str, Any] | None = None,
) -> QueryPlan:
    """Fill single-match semantics, comparison, and ranking sort before validation."""
    plan = normalize_comparison_plan(plan, question=question)
    plan = _normalize_plan_sort_for_ranking(plan)
    if not plan.metrics and semantic_catalog:
        candidates = _question_metric_candidates(question, semantic_catalog)
        if len(candidates) == 1:
            plan = plan.model_copy(deep=True)
            plan.metrics = [candidates[0]]
    if plan.intent == "detail" and not plan.dimensions and semantic_catalog:
        # A row-listing question names several raw fields at once ("customer
        # name, invoice number, invoice value"), unlike aggregation's
        # single-metric case above — so fill every explicit catalog match
        # rather than requiring exactly one.
        dim_candidates = match_catalog_names(question, semantic_catalog.get("dimensions"))
        if dim_candidates:
            plan = plan.model_copy(deep=True)
            plan.dimensions = dim_candidates
    if (
        plan.time
        and plan.time.range not in {None, "all"}
        and not plan.time.field
        and semantic_catalog
    ):
        _, _, date_dims = _catalog_names(semantic_catalog)
        if len(date_dims) == 1:
            plan = plan.model_copy(deep=True)
            plan.time = plan.time.model_copy(update={"field": date_dims[0]})
    return plan


def _rank_by_relevance(text: str, names: list[str]) -> list[str]:
    """Order candidate names by keyword overlap with ``text``.

    Used whenever a clarification has to show choices with no confident
    match at all — showing the first N catalog entries in arbitrary
    dict/insertion order (e.g. "Available Hours", "Downtime Hours" for a
    question about "sales amount") is a real, observed failure: it looks
    like a considered suggestion but is actually random. Ranking by overlap
    at least surfaces the plausible-sounding options first, even when
    nothing was confident enough to auto-select — this never changes
    *whether* the system asks, only what it offers to pick from.
    """
    tokens = {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}
    if not tokens:
        return names

    def score(name: str) -> int:
        name_tokens = set(re.findall(r"[a-z0-9]+", name.lower()))
        return len(tokens & name_tokens)

    return sorted(names, key=score, reverse=True)


def _unresolved_field_clarification(
    field_name: str, *, kind: str, catalog_names: list[str], field_kind_label: str
) -> QueryClarification:
    """A friendly clarification for a field the plan named that isn't in
    the confirmed catalog. Never surface a raw "Unknown metric: x" /
    "Unknown dimension: x" validation string directly as a chat answer —
    that's an internal error label, not something a business user asked
    for or can act on.
    """
    if catalog_names:
        ranked = _rank_by_relevance(field_name, catalog_names)
        return QueryClarification(
            kind=kind,
            message=f'I don\'t have a confirmed {field_kind_label} for "{field_name}" yet. Did you mean one of these?',
            choices=[
                QueryClarificationChoice(id=str(i + 1), label=name)
                for i, name in enumerate(ranked[:6])
            ],
        )
    return QueryClarification(
        kind=kind,
        message=(
            f'I don\'t have a confirmed {field_kind_label} for "{field_name}" yet, and no '
            f"{field_kind_label}s are confirmed for this connection. Ask your admin to confirm "
            "some in Semantic settings, or rephrase using an exact column name."
        ),
    )


def validate_query_plan(
    plan: QueryPlan,
    *,
    question: str = "",
    semantic_catalog: dict[str, Any] | None = None,
) -> PlanValidationResult:
    """Validate a structured plan against the semantic catalog before execution."""
    metric_map, dim_map, date_dims = _catalog_names(semantic_catalog)
    has_catalog = bool(metric_map)
    ambiguous_dims = ambiguous_alias_targets((semantic_catalog or {}).get("dimensions"))
    ambiguous_metrics_map = ambiguous_alias_targets((semantic_catalog or {}).get("metrics"))

    if plan.intent == "detail" and not plan.metrics and not plan.dimensions:
        # Nothing matched any field the question named — falling back to
        # SELECT * FROM <arbitrary table> would silently return an
        # unrelated table's columns, same failure shape as guessing a
        # metric. Ask instead. Checked before the `has_catalog` gate below
        # since "nothing resolved" is a real gap whether or not this
        # connection has any confirmed metrics — a connection with no
        # confirmed metrics can still have confirmed dimensions, and either
        # way an ungrounded detail query shouldn't run silently.
        dim_names = _rank_by_relevance(question, list(dim_map.values()))[:6]
        return PlanValidationResult(
            ok=False,
            clarification=QueryClarification(
                kind="pick_dimension",
                message="Which fields should I include?",
                choices=[
                    QueryClarificationChoice(id=str(i + 1), label=name)
                    for i, name in enumerate(dim_names)
                ],
            ),
        )

    if not has_catalog:
        return PlanValidationResult(ok=True)

    errors: list[str] = []

    if plan.intent in {"aggregation", "comparison", "share_of_total"} and not plan.metrics:
        candidates = _question_metric_candidates(question, semantic_catalog)
        if len(candidates) > 1:
            return PlanValidationResult(
                ok=False,
                clarification=QueryClarification(
                    kind="pick_metric",
                    message="Which metric should I use?",
                    choices=[
                        QueryClarificationChoice(id=str(i + 1), label=name)
                        for i, name in enumerate(candidates[:6])
                    ],
                ),
            )
        if not candidates:
            metric_names = _rank_by_relevance(question, list(metric_map.values()))[:6]
            return PlanValidationResult(
                ok=False,
                clarification=QueryClarification(
                    kind="pick_metric",
                    message="Which metric should I use for this question?",
                    choices=[
                        QueryClarificationChoice(id=str(i + 1), label=name)
                        for i, name in enumerate(metric_names)
                    ],
                ),
            )
        errors.append("Missing metric for aggregation/comparison intent.")

    for metric in plan.metrics:
        if metric.lower() in metric_map:
            continue
        targets = ambiguous_metrics_map.get(metric.lower())
        if targets:
            return PlanValidationResult(
                ok=False,
                clarification=QueryClarification(
                    kind="pick_metric",
                    message="Which metric should I use?",
                    choices=[
                        QueryClarificationChoice(id=str(i + 1), label=name)
                        for i, name in enumerate(targets[:6])
                    ],
                ),
            )
        return PlanValidationResult(
            ok=False,
            clarification=_unresolved_field_clarification(
                metric, kind="pick_metric", catalog_names=list(metric_map.values()), field_kind_label="metric"
            ),
        )

    for dim in plan.dimensions:
        if dim.lower() in dim_map:
            continue
        targets = ambiguous_dims.get(dim.lower())
        if targets:
            return PlanValidationResult(
                ok=False,
                clarification=QueryClarification(
                    kind="pick_dimension",
                    message="Which field should I use?",
                    choices=[
                        QueryClarificationChoice(id=str(i + 1), label=name)
                        for i, name in enumerate(targets[:6])
                    ],
                ),
            )
        return PlanValidationResult(
            ok=False,
            clarification=_unresolved_field_clarification(
                dim, kind="pick_dimension", catalog_names=list(dim_map.values()), field_kind_label="field"
            ),
        )

    for filt in plan.filters:
        field = filt.field
        if field.lower() not in dim_map and field.lower() not in metric_map:
            return PlanValidationResult(
                ok=False,
                clarification=_unresolved_field_clarification(
                    field,
                    kind="pick_dimension",
                    catalog_names=list(dim_map.values()) + list(metric_map.values()),
                    field_kind_label="field",
                ),
            )

    ambiguous_metrics = _question_metric_candidates(question, semantic_catalog)
    if len(ambiguous_metrics) > 1 and len(plan.metrics) <= 1:
        planned = {m.lower() for m in plan.metrics}
        if not planned or planned <= {ambiguous_metrics[0].lower()}:
            return PlanValidationResult(
                ok=False,
                clarification=QueryClarification(
                    kind="pick_metric",
                    message="Your question could refer to more than one metric. Which should I use?",
                    choices=[
                        QueryClarificationChoice(id=str(i + 1), label=name)
                        for i, name in enumerate(ambiguous_metrics[:6])
                    ],
                ),
            )

    if plan.time and plan.time.field:
        field = plan.time.field
        if field.lower() not in dim_map and field.lower() not in metric_map:
            return PlanValidationResult(
                ok=False,
                clarification=_unresolved_field_clarification(
                    field, kind="pick_date_field", catalog_names=date_dims, field_kind_label="date field"
                ),
            )
    elif plan.time and plan.time.range not in {None, "all"} and len(date_dims) > 1 and not plan.time.field:
        return PlanValidationResult(
            ok=False,
            clarification=QueryClarification(
                kind="pick_date_field",
                message="Which date field should I use for the time period?",
                choices=[
                    QueryClarificationChoice(id=str(i + 1), label=name)
                    for i, name in enumerate(date_dims[:6])
                ],
            ),
        )

    if plan.intent == "comparison":
        if plan.comparison is None:
            errors.append("Comparison intent requires comparison specification.")
        elif plan.comparison.type == "entity_vs_entity":
            if not plan.comparison.entities or len(plan.comparison.entities) < 2:
                errors.append("Entity comparison requires two entities.")
            if not plan.comparison.entity_dimension and not plan.dimensions:
                return PlanValidationResult(
                    ok=False,
                    clarification=QueryClarification(
                        kind="pick_dimension",
                        message="Which dimension should I use to compare those entities?",
                        choices=[
                            QueryClarificationChoice(id=str(i + 1), label=name)
                            for i, name in enumerate(_rank_by_relevance(question, list(dim_map.values()))[:6])
                        ],
                    ),
                )
        elif plan.time is None and plan.comparison.type != "entity_vs_entity":
            errors.append("Comparison intent requires an explicit time period.")
        elif plan.comparison.type == "previous_period" and plan.time:
            if derive_comparison_period(plan.time) is None and plan.comparison.comparison_time is None:
                errors.append("Cannot derive previous period from current time context.")

    if plan.intent == "share_of_total" and not plan.dimensions:
        errors.append("Share-of-total requires a grouping dimension.")

    if plan.limit is not None and plan.limit < 1:
        errors.append("Invalid limit.")

    if errors:
        return PlanValidationResult(
            ok=False,
            errors=errors,
            clarification=QueryClarification(
                kind="invalid_plan",
                message=errors[0],
            ),
        )

    return PlanValidationResult(ok=True)


def verify_sql_reflects_plan(
    sql: str,
    plan: QueryPlan,
    semantic_catalog: dict[str, Any] | None,
    *,
    db_type: str = "postgres",
) -> PlanValidationResult:
    """Check compiled SQL reflects the resolved plan (AST-first, string fallback)."""
    from app.agent.sql_plan_verification import verify_sql_plan_ast

    ast_result = verify_sql_plan_ast(sql, plan, semantic_catalog, db_type=db_type)
    if not ast_result.ok:
        return ast_result

    if not sql or not semantic_catalog:
        return PlanValidationResult(ok=True)

    from app.agent.sql_compiler import _catalog_maps

    metrics, dimensions = _catalog_maps(semantic_catalog)
    upper = sql.upper()
    lower = sql.lower()

    for metric in plan.metrics:
        key = metric.lower()
        expr = metrics.get(key) or metric
        alias = re.sub(r"[^a-zA-Z0-9_]+", "_", metric).strip("_").upper() or "METRIC"
        expr_upper = expr.upper()
        if expr_upper not in upper and alias not in upper and metric.upper() not in upper:
            return PlanValidationResult(
                ok=False,
                errors=[f"SQL does not appear to use metric {metric}."],
                clarification=QueryClarification(
                    kind="invalid_plan",
                    message=(
                        "I could not compile a query that matches the requested metric. "
                        "Try rephrasing or pick a metric from your semantic catalog."
                    ),
                ),
            )

    for dim in plan.dimensions:
        key = dim.lower()
        expr = dimensions.get(key) or dim
        alias = re.sub(r"[^a-zA-Z0-9_]+", "_", dim).strip("_").upper() or "DIMENSION"
        if expr.upper() not in upper and alias not in upper and dim.upper() not in upper:
            return PlanValidationResult(
                ok=False,
                errors=[f"SQL does not appear to group by {dim}."],
                clarification=QueryClarification(
                    kind="invalid_plan",
                    message=f"I could not compile a query grouped by {dim}.",
                ),
            )

    if plan.limit is not None:
        if "LIMIT" not in upper and "TOP" not in upper:
            return PlanValidationResult(
                ok=False,
                errors=["SQL missing expected row limit."],
                clarification=QueryClarification(
                    kind="invalid_plan",
                    message="The ranking limit could not be applied safely.",
                ),
            )

    if plan.sort:
        primary = plan.sort[0]
        if primary.direction == "desc" and "DESC" not in upper:
            return PlanValidationResult(ok=False, errors=["SQL missing DESC sort for ranking."])
        if primary.direction == "asc" and "ASC" not in upper and "DESC" in upper:
            return PlanValidationResult(ok=False, errors=["SQL sort direction mismatch."])

    if plan.comparison and plan.intent == "comparison":
        if "current_" not in lower and "comparison_" not in lower and "entity_a_" not in lower:
            return PlanValidationResult(
                ok=False,
                errors=["SQL missing dual-period comparison columns."],
            )

    if plan.intent == "share_of_total" and "share_pct" not in lower and "share_" not in lower:
        return PlanValidationResult(ok=False, errors=["SQL missing share-of-total column."])

    return PlanValidationResult(ok=True)


def format_clarification_answer(clarification: QueryClarification) -> str:
    lines = [clarification.message]
    if clarification.choices:
        lines.append("")
        for choice in clarification.choices:
            lines.append(f"- {choice.id}. {choice.label}")
    return "\n".join(lines)


# Backward-compatible re-export; canonical implementation is time_resolution.resolve_time_phrase
