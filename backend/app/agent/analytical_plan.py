"""Structured analytical plans for dashboard creation (no raw SQL).

The LLM (or a heuristic) selects analyses from a DatasetProfile. Each analysis
is compiled to SQL through the existing QueryPlan compiler.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app.agent.dataset_profiler import (
    DatasetProfile,
    ProfileMetric,
)
from app.agent.analysis_quality import (
    HIGH_UNIQUENESS,
    MIN_TREND_POINTS,
    evaluate_plan_analyses,
    infer_value_kind,
)
from app.agent.file_dashboard_sql import (
    adapt_file_dashboard_spec,
    is_file_sql_engine,
    rewrite_sql_for_file_engine,
)
from app.agent.query_plan import QueryPlan, SortSpec, TimeSpec
from app.agent.sql_compiler import compile_query_plan
from app.tools.sql_safety import SQLSafetyPipeline

logger = logging.getLogger(__name__)

Visualization = Literal["kpi", "bar", "line", "pie", "table"]
Aggregation = Literal["sum", "avg", "count", "count_distinct", "none"]
Grain = Literal["day", "week", "month", "quarter", "year"]

_MAX_ANALYSES = 6
_MIN_ANALYSES = 2


class AnalysisSpec(BaseModel):
    purpose: str
    metric: str
    metric_expression: str = ""
    dimension: str | None = None
    grain: Grain | None = None
    time_field: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    aggregation: Aggregation = "sum"
    visualization: Visualization = "kpi"
    title: str
    id: str = ""


class AnalyticalPlan(BaseModel):
    title: str = "Insight Dashboard"
    source_table: str | None = None
    analyses: list[AnalysisSpec] = Field(default_factory=list)
    build_mode: str = "analytical_heuristic"
    decisions: list[dict[str, Any]] = Field(default_factory=list)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:48] or uuid4().hex[:8]


def _humanize(name: str) -> str:
    return re.sub(r"[_\s]+", " ", (name or "").strip()).title()


def _parse_plan_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        text = "\n".join(body).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def bind_visualization(
    analysis: AnalysisSpec,
    profile: DatasetProfile,
) -> AnalysisSpec:
    """Choose visualization from purpose, grain, cardinality, and metric kind."""
    updated = analysis.model_copy(deep=True)
    dimension = (
        profile.dimension_by_name(updated.dimension) if updated.dimension else None
    )
    date_field = (
        profile.dimension_by_name(updated.time_field)
        if updated.time_field
        else (profile.dates[0] if profile.dates else None)
    )

    if updated.visualization == "line" or (updated.grain and date_field is not None and not updated.dimension):
        if date_field is None:
            updated.visualization = "bar" if updated.dimension else "kpi"
            return updated
        updated.visualization = "line"
        updated.dimension = None
        updated.time_field = date_field.column
        updated.grain = updated.grain or "month"
        return updated

    if not updated.dimension:
        updated.visualization = "kpi"
        return updated

    card = dimension.cardinality if dimension is not None else None
    if updated.visualization == "pie":
        # A pie showing composition of an absolute total (e.g. revenue by
        # region) is standard BI practice — only cardinality (too many
        # slices to read) should downgrade it, not the metric's kind.
        if card is not None and card > 8:
            updated.visualization = "bar"
        return updated

    if card is not None and card > 40:
        updated.visualization = "table"
        return updated

    if updated.visualization == "line" and date_field is None:
        updated.visualization = "bar"
        return updated

    if updated.visualization in {"kpi", "line"}:
        updated.visualization = "bar"
    elif updated.visualization not in {"bar", "pie", "table"}:
        updated.visualization = "bar"
    return updated


def _coerce_aggregation(metric: ProfileMetric, requested: str | None) -> Aggregation:
    if metric.kind == "rate":
        return "avg"
    if metric.kind == "distinct":
        return "count_distinct"
    if metric.kind in {"count", "row_count"}:
        return "count" if metric.aggregation == "count" else "sum"
    if metric.kind == "derived":
        return "none"
    if metric.kind == "duration":
        return "count"
    requested_norm = (requested or metric.aggregation or "sum").lower()
    if requested_norm in {"sum", "avg", "count", "count_distinct", "none"}:
        if metric.kind == "additive" and requested_norm == "avg":
            return "avg"
        if metric.kind == "additive":
            return "sum"
        return requested_norm  # type: ignore[return-value]
    return metric.aggregation


def validate_analyses_against_profile(
    analyses: list[AnalysisSpec],
    profile: DatasetProfile,
) -> list[AnalysisSpec]:
    """Drop analyses that invent columns or use invalid aggregations."""
    valid: list[AnalysisSpec] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in analyses:
        metric = profile.metric_by_name(raw.metric)
        if metric is None:
            continue
        if metric.kind == "duration":
            continue
        dimension_name = raw.dimension
        if dimension_name:
            dim = profile.dimension_by_name(dimension_name)
            if dim is None:
                continue
            dimension_name = dim.name
        time_field = raw.time_field
        if raw.grain or raw.visualization == "line":
            date = (
                profile.dimension_by_name(time_field)
                if time_field
                else (profile.dates[0] if profile.dates else None)
            )
            if date is None:
                if raw.visualization == "line" and not dimension_name:
                    continue
                time_field = None
            else:
                time_field = date.column
        aggregation = _coerce_aggregation(metric, raw.aggregation)
        spec = AnalysisSpec(
            purpose=raw.purpose or metric.kind,
            metric=metric.name,
            metric_expression=metric.expression,
            dimension=dimension_name,
            grain=raw.grain if time_field else None,
            time_field=time_field,
            filters=list(raw.filters or []),
            aggregation=aggregation,
            visualization=raw.visualization or "kpi",
            title=raw.title or metric.name,
            id=raw.id or _slug(f"{raw.visualization}-{metric.name}-{dimension_name or ''}"),
        )
        spec = bind_visualization(spec, profile)
        key = (
            spec.metric.lower(),
            (spec.dimension or "").lower(),
            spec.visualization,
            spec.grain or "",
        )
        if key in seen:
            continue
        seen.add(key)
        valid.append(spec)
        if len(valid) >= _MAX_ANALYSES:
            break
    return valid


def _breakdown_visualization(dim: ProfileField) -> Visualization:
    """A handful of categories reads better as a share-of-total pie than
    a bar chart with only a few bars."""
    if dim.cardinality is not None and 2 <= dim.cardinality <= 6:
        return "pie"
    return "bar"


def heuristic_analytical_plan(
    profile: DatasetProfile,
    *,
    question: str = "",
    focus_text: str = "",
) -> AnalyticalPlan:
    """Deterministic plan from profile candidates (no LLM, no invented columns)."""
    analyses: list[AnalysisSpec] = []
    additive = sorted(
        [*profile.counts, *profile.measures],
        key=lambda m: m.score,
        reverse=True,
    )
    rates = list(profile.rates)
    derived = list(profile.derived_metrics)
    # Business KPIs discovered from column pairs (achievement %, rejection %,
    # on-time %, margin %, value per unit) — the numbers an analyst reports.
    pattern_kpis = sorted(
        [m for m in derived if m.kind == "derived" and m.unit],
        key=lambda m: m.score,
        reverse=True,
    )
    dimensions = [
        d
        for d in profile.dimensions
        if not (
            d.uniqueness_ratio is not None
            and d.uniqueness_ratio >= HIGH_UNIQUENESS
            and (d.cardinality is None or d.cardinality >= 15)
        )
        and d.cardinality != 1
    ]
    dates = [
        d
        for d in profile.dates
        if d.date_bucket_count is None or d.date_bucket_count >= MIN_TREND_POINTS
    ]
    primary_measure = additive[0] if additive else None
    primary_dim = dimensions[0] if dimensions else None

    def _add(spec: AnalysisSpec) -> None:
        if len(analyses) >= _MAX_ANALYSES:
            return
        analyses.append(spec)

    if primary_measure is not None:
        _add(
            AnalysisSpec(
                purpose="headline_measure",
                metric=primary_measure.name,
                metric_expression=primary_measure.expression,
                aggregation=_coerce_aggregation(primary_measure, None),
                visualization="kpi",
                title=primary_measure.name
                if primary_measure.kind != "additive" or primary_measure.origin == "semantic"
                else f"Total {primary_measure.name}",
                id=f"kpi-{_slug(primary_measure.name)}",
            )
        )
    elif not additive:
        _add(
            AnalysisSpec(
                purpose="row_count",
                metric="Record Count",
                metric_expression="COUNT(*)",
                aggregation="count",
                visualization="kpi",
                title="Total Records",
                id="kpi-records",
            )
        )

    if pattern_kpis:
        top_kpi = pattern_kpis[0]
        _add(
            AnalysisSpec(
                purpose="business_kpi",
                metric=top_kpi.name,
                metric_expression=top_kpi.expression,
                aggregation="none",
                visualization="kpi",
                title=top_kpi.name,
                id=f"kpi-{_slug(top_kpi.name)}",
            )
        )

    # Breakdown chart and trend line come right after the headline KPI,
    # ahead of the secondary rate/distinct/ratio KPIs below. `_add` caps at
    # _MAX_ANALYSES, and — for a whole-database dashboard — each section
    # gets truncated again to `widgets_per_table` (see
    # _build_dashboard_section), keeping only the *first* N analyses in
    # insertion order. With charts/trend added last, a section with a
    # couple of rate/distinct measures would fill its whole budget with
    # KPI cards before ever reaching them, so a "comprehensive" dashboard
    # ended up as a wall of numbers with almost no charts and no trends at
    # all. Visual variety earns its place ahead of a 4th or 5th KPI card.
    if primary_measure is not None and primary_dim is not None:
        _add(
            AnalysisSpec(
                purpose="breakdown",
                metric=primary_measure.name,
                metric_expression=primary_measure.expression,
                dimension=primary_dim.name,
                aggregation=_coerce_aggregation(primary_measure, None),
                visualization=_breakdown_visualization(primary_dim),
                title=f"{primary_measure.name} by {primary_dim.name}",
                id=f"chart-{primary_dim.column}",
            )
        )

    if primary_measure is not None and dates:
        date = dates[0]
        _add(
            AnalysisSpec(
                purpose="trend",
                metric=primary_measure.name,
                metric_expression=primary_measure.expression,
                grain="month",
                time_field=date.column,
                aggregation=_coerce_aggregation(primary_measure, None),
                visualization="line",
                title=f"{primary_measure.name} Over Time",
                id=f"trend-{date.column}",
            )
        )
    elif dates and analyses:
        _add(
            AnalysisSpec(
                purpose="trend",
                metric=analyses[0].metric,
                metric_expression=analyses[0].metric_expression,
                grain="month",
                time_field=dates[0].column,
                aggregation=analyses[0].aggregation,
                visualization="line",
                title="Trend Over Time",
                id=f"trend-{dates[0].column}",
            )
        )

    if pattern_kpis and dimensions:
        # Where is the KPI good or bad? Break it down by the most entity-like
        # dimension (plant, supplier, unit...) — ratio of sums stays correct
        # per group.
        # Prefer a different dimension table than the main breakdown (e.g.
        # "by Unit" next to "by Product"), not another attribute of the same
        # entity ("Product Code" next to "Product Name").
        entity_dim = next(
            (d for d in dimensions if primary_dim is None or d.table != primary_dim.table),
            next((d for d in dimensions if d is not primary_dim), primary_dim),
        )
        top_kpi = pattern_kpis[0]
        _add(
            AnalysisSpec(
                purpose="business_kpi_breakdown",
                metric=top_kpi.name,
                metric_expression=top_kpi.expression,
                dimension=entity_dim.name,
                aggregation="none",
                visualization="bar",
                title=f"{top_kpi.name} by {entity_dim.name}",
                id=f"chart-kpi-{_slug(top_kpi.name)}-{entity_dim.column}",
            )
        )
    for extra_kpi in pattern_kpis[1:3]:
        _add(
            AnalysisSpec(
                purpose="business_kpi",
                metric=extra_kpi.name,
                metric_expression=extra_kpi.expression,
                aggregation="none",
                visualization="kpi",
                title=extra_kpi.name,
                id=f"kpi-{_slug(extra_kpi.name)}",
            )
        )

    for rate in rates[:2]:
        _add(
            AnalysisSpec(
                purpose="rate_kpi",
                metric=rate.name,
                metric_expression=rate.expression,
                aggregation="avg",
                visualization="kpi",
                title=rate.name,
                id=f"kpi-{_slug(rate.name)}",
            )
        )

    distincts = [m for m in derived if m.kind == "distinct"]
    if distincts:
        metric = distincts[0]
        _add(
            AnalysisSpec(
                purpose="entity_count",
                metric=metric.name,
                metric_expression=metric.expression,
                aggregation="count_distinct",
                visualization="kpi",
                title=metric.name,
                id=f"kpi-{_slug(metric.name)}",
            )
        )

    ratios = [m for m in derived if m.kind == "derived" and not m.unit]
    if ratios and len(analyses) < _MAX_ANALYSES:
        metric = ratios[0]
        _add(
            AnalysisSpec(
                purpose="derived_ratio",
                metric=metric.name,
                metric_expression=metric.expression,
                aggregation="none",
                visualization="kpi",
                title=metric.name,
                id=f"kpi-{_slug(metric.name)}",
            )
        )

    if len(dimensions) > 1 and primary_measure is not None and len(analyses) < _MAX_ANALYSES:
        extra_dim = dimensions[1]
        _add(
            AnalysisSpec(
                purpose="secondary_breakdown",
                metric=primary_measure.name,
                metric_expression=primary_measure.expression,
                dimension=extra_dim.name,
                aggregation=_coerce_aggregation(primary_measure, None),
                visualization=_breakdown_visualization(extra_dim),
                title=f"{primary_measure.name} by {extra_dim.name}",
                id=f"chart-{extra_dim.column}",
            )
        )

    if primary_measure is not None and primary_dim is not None and len(analyses) < _MAX_ANALYSES:
        _add(
            AnalysisSpec(
                purpose="ranked_table",
                metric=primary_measure.name,
                metric_expression=primary_measure.expression,
                dimension=primary_dim.name,
                aggregation=_coerce_aggregation(primary_measure, None),
                visualization="table",
                title=f"Top {primary_dim.name} by {primary_measure.name}",
                id=f"table-{_slug(primary_dim.column)}",
            )
        )

    # Inject row-count metric into a temporary profile copy for validation when used.
    plan_profile = profile
    if any(a.metric == "Record Count" for a in analyses) and profile.metric_by_name("Record Count") is None:
        plan_profile = profile.model_copy(deep=True)
        plan_profile.counts.append(
            ProfileMetric(
                name="Record Count",
                expression="COUNT(*)",
                kind="row_count",
                aggregation="count",
                score=1,
            )
        )

    validated = validate_analyses_against_profile(analyses, plan_profile)
    kept, decisions = evaluate_plan_analyses(
        validated, plan_profile, source="heuristic"
    )
    title = "Insight Dashboard"
    if profile.source_table:
        title = f"{_humanize(profile.source_table)} Dashboard"
    if question.strip() and "dashboard" not in question.lower():
        title = "Insight Dashboard"
    return AnalyticalPlan(
        title=title,
        source_table=profile.source_table,
        analyses=kept,
        build_mode="analytical_heuristic",
        decisions=[item.model_dump(mode="json") for item in decisions],
    )


def parse_llm_analytical_plan(
    raw: str,
    profile: DatasetProfile,
) -> AnalyticalPlan | None:
    data = _parse_plan_json(raw)
    if not data:
        return None
    items = data.get("analyses") or data.get("widgets")
    if not isinstance(items, list) or not items:
        return None
    parsed: list[AnalysisSpec] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        metric = str(item.get("metric") or item.get("name") or "").strip()
        if not metric:
            continue
        viz = str(item.get("visualization") or item.get("chart") or item.get("type") or "kpi")
        if viz in {"chart"}:
            viz = str(item.get("chart") or "bar")
        if viz not in {"kpi", "bar", "line", "pie", "table"}:
            viz = "kpi"
        grain_raw = item.get("grain")
        grain = grain_raw if grain_raw in {"day", "week", "month", "quarter", "year"} else None
        agg_raw = str(item.get("aggregation") or "sum").lower()
        if agg_raw not in {"sum", "avg", "count", "count_distinct", "none"}:
            agg_raw = "sum"
        dim = item.get("dimension")
        try:
            parsed.append(
                AnalysisSpec(
                    purpose=str(item.get("purpose") or viz),
                    metric=metric,
                    dimension=str(dim).strip() if dim else None,
                    grain=grain,
                    time_field=str(item.get("time_field") or "") or None,
                    aggregation=agg_raw,  # type: ignore[arg-type]
                    visualization=viz,  # type: ignore[arg-type]
                    title=str(item.get("title") or metric),
                    id=str(item.get("id") or f"analysis-{idx}"),
                )
            )
        except Exception:  # noqa: BLE001
            continue
    validated = validate_analyses_against_profile(parsed, profile)
    if not validated:
        return None
    kept, decisions = evaluate_plan_analyses(validated, profile, source="openai")
    if not kept:
        return None
    return AnalyticalPlan(
        title=str(data.get("title") or "Insight Dashboard"),
        source_table=profile.source_table,
        analyses=kept,
        build_mode="analytical_llm",
        decisions=[item.model_dump(mode="json") for item in decisions],
    )


def analysis_to_query_plan(
    analysis: AnalysisSpec,
    profile: DatasetProfile,
) -> tuple[QueryPlan, dict[str, Any]]:
    """Build a QueryPlan plus a one-analysis semantic catalog (aliases value/category)."""
    metric = profile.metric_by_name(analysis.metric)
    if metric is None:
        raise ValueError(f"Unknown metric: {analysis.metric}")

    catalog: dict[str, Any] = {
        "metrics": [{"name": "value", "expression": metric.expression}],
        "dimensions": [],
        "relationships": [],
    }
    dimensions: list[str] = []
    if analysis.dimension and analysis.visualization != "kpi":
        dim = profile.dimension_by_name(analysis.dimension)
        if dim is None:
            raise ValueError(f"Unknown dimension: {analysis.dimension}")
        catalog["dimensions"].append({"name": "category", "expression": dim.expression})
        dimensions = ["category"]

    time_spec: TimeSpec | None = None
    if analysis.grain or analysis.visualization == "line":
        date = (
            profile.dimension_by_name(analysis.time_field)
            if analysis.time_field
            else (profile.dates[0] if profile.dates else None)
        )
        if date is not None:
            catalog["dimensions"].append({"name": "event_date", "expression": date.expression})
            time_spec = TimeSpec(field="event_date", grain=analysis.grain or "month")

    limit = None
    if analysis.visualization in {"bar", "pie"}:
        limit = 8 if analysis.visualization == "pie" else 15
    elif analysis.visualization == "line":
        limit = 24
    elif analysis.visualization == "table":
        limit = 20

    sort = [SortSpec(field="value", direction="desc")]
    if time_spec is not None and not dimensions:
        # Newest periods first so LIMIT keeps the latest 24 months, not the
        # oldest; charts and facts re-sort chronologically.
        sort = [SortSpec(field="event_date", direction="desc")]

    plan = QueryPlan(
        intent="aggregation",
        metrics=["value"],
        dimensions=dimensions,
        time=time_spec,
        sort=sort,
        limit=limit,
    )
    return plan, catalog


def _compile_one_analysis(
    analysis: AnalysisSpec,
    *,
    profile: DatasetProfile,
    schema: dict[str, Any],
    db_type: str,
) -> dict[str, Any] | None:
    try:
        query_plan, catalog = analysis_to_query_plan(analysis, profile)
        sql = compile_query_plan(query_plan, catalog, schema, db_type)
    except Exception as exc:  # noqa: BLE001
        logger.info("analytical_compile_failed purpose=%s error=%s", analysis.purpose, exc)
        return None
    if analysis.visualization == "line" and re.search(r"\bAS period\b", sql, re.I):
        sql = re.sub(
            r"\sORDER BY\s+.+?(?=(\sLIMIT\b|$))",
            " ORDER BY period DESC",
            sql,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
    if is_file_sql_engine(db_type):
        sql = rewrite_sql_for_file_engine(sql)
    try:
        sql = SQLSafetyPipeline(schema=schema, db_type=db_type).validate(sql)
    except Exception as exc:  # noqa: BLE001
        logger.info("analytical_sql_invalid purpose=%s error=%s", analysis.purpose, exc)
        return None

    viz = analysis.visualization
    if viz == "kpi":
        wtype, chart = "kpi", None
    elif viz == "table":
        wtype, chart = "table", None
    elif viz == "pie":
        wtype, chart = "pie", "pie"
    else:
        wtype, chart = "chart", viz
    metric = profile.metric_by_name(analysis.metric)
    dim = profile.dimension_by_name(analysis.dimension) if analysis.dimension else None
    value_kind = infer_value_kind(
        metric_kind=metric.kind if metric else None,
        aggregation=analysis.aggregation,
    )
    if metric is not None and metric.unit == "percent":
        value_kind = "percentage"
    return {
        "id": analysis.id or _slug(analysis.title),
        "type": wtype,
        "title": analysis.title,
        "sql": sql,
        "chart": chart,
        "rows": [],
        "purpose": analysis.purpose,
        "metric": analysis.metric,
        "metric_expression": analysis.metric_expression or (metric.expression if metric else ""),
        "dimension": analysis.dimension,
        "aggregation": analysis.aggregation,
        "value_kind": value_kind,
        "polarity": metric.polarity if metric else None,
        "quality": {
            "metric_kind": metric.kind if metric else None,
            "metric_column": metric.column if metric else None,
            "dimension_column": dim.column if dim else None,
            "dimension_table": dim.table if dim else None,
            "cardinality": dim.cardinality if dim else None,
            "uniqueness_ratio": dim.uniqueness_ratio if dim else None,
            "aggregation": analysis.aggregation,
            "value_kind": value_kind,
            "date_bucket_count": (
                next(
                    (
                        d.date_bucket_count
                        for d in profile.dates
                        if analysis.time_field
                        and d.column.lower() == str(analysis.time_field).lower()
                    ),
                    profile.dates[0].date_bucket_count if profile.dates else None,
                )
                if analysis.visualization == "line"
                else None
            ),
            "part_of": next(
                (
                    p.whole
                    for p in profile.part_whole_pairs
                    if metric
                    and metric.column
                    and p.part.lower() == metric.column.lower()
                ),
                None,
            ),
        },
    }


def compile_analytical_plan(
    plan: AnalyticalPlan,
    *,
    profile: DatasetProfile,
    schema: dict[str, Any],
    db_type: str,
) -> dict[str, Any]:
    """Compile each analysis to a dashboard widget spec (SQL via existing compiler)."""
    widgets: list[dict[str, Any]] = []
    for analysis in plan.analyses:
        widget = _compile_one_analysis(
            analysis,
            profile=profile,
            schema=schema,
            db_type=db_type,
        )
        if widget is None:
            continue
        widgets.append(widget)

    spec = {
        "title": plan.title,
        "source_table": plan.source_table,
        "widgets": widgets,
        "build_mode": plan.build_mode,
        "analytical_plan": plan.model_dump(mode="json"),
        "analysis_decisions": list(plan.decisions),
        "part_whole_pairs": [p.model_dump() for p in profile.part_whole_pairs],
        "dataset_row_count": profile.row_count,
    }
    if is_file_sql_engine(db_type):
        adapted = adapt_file_dashboard_spec(spec, schema)
        if adapted is not None:
            adapted["build_mode"] = plan.build_mode
            adapted["analytical_plan"] = spec["analytical_plan"]
            adapted["source_table"] = plan.source_table
            adapted["analysis_decisions"] = spec["analysis_decisions"]
            adapted["part_whole_pairs"] = spec.get("part_whole_pairs") or []
            adapted["dataset_row_count"] = spec.get("dataset_row_count")
            return adapted
        spec["widgets"] = []
    return spec


def format_profile_for_prompt(profile: DatasetProfile) -> dict[str, str]:
    def _fmt_num(value: Any) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, float):
            return f"{value:.6g}"
        return str(value)

    def _field_line(item: Any, *, role: str) -> str:
        bits = [
            f"- {item.name} (column: {item.column}",
            f"type = {role}",
        ]
        if getattr(item, "data_type", None):
            bits.append(f"data_type = {item.data_type}")
        card = getattr(item, "distinct_count", None)
        if card is None:
            card = getattr(item, "cardinality", None)
        bits.append(f"distinct_count = {_fmt_num(card)}")
        bits.append(f"uniqueness_ratio = {_fmt_num(getattr(item, 'uniqueness_ratio', None))}")
        bits.append(f"null_count = {_fmt_num(getattr(item, 'null_count', None))}")
        bits.append(f"null_ratio = {_fmt_num(getattr(item, 'null_ratio', None))}")
        samples = list(getattr(item, "sample_values", None) or [])
        if samples:
            bits.append("samples = " + ", ".join(str(s) for s in samples[:5]))
        if role in {"numeric", "measure"}:
            bits.append(f"min = {_fmt_num(getattr(item, 'min_value', None))}")
            bits.append(f"max = {_fmt_num(getattr(item, 'max_value', None))}")
        if role == "date":
            bits.append(f"date_min = {_fmt_num(getattr(item, 'date_min', None))}")
            bits.append(f"date_max = {_fmt_num(getattr(item, 'date_max', None))}")
            bits.append(f"date_bucket_count = {_fmt_num(getattr(item, 'date_bucket_count', None))}")
        return ", ".join(bits) + ")"

    def _metric_line(item: Any) -> str:
        bits = [
            f"- {item.name} (kind: {item.kind}",
            f"aggregation: {item.aggregation}",
        ]
        if item.column:
            bits.append(f"column: {item.column}")
        bits.append(f"distinct_count = {_fmt_num(item.distinct_count)}")
        bits.append(f"null_ratio = {_fmt_num(item.null_ratio)}")
        bits.append(f"min = {_fmt_num(item.min_value)}")
        bits.append(f"max = {_fmt_num(item.max_value)}")
        return ", ".join(bits) + ")"

    def _lines(items: list[Any], *, kind: str, role: str = "") -> str:
        if not items:
            return "(none)"
        rows: list[str] = []
        for item in items[:12]:
            if kind == "field":
                rows.append(_field_line(item, role=role or item.kind))
            else:
                rows.append(_metric_line(item))
        return "\n".join(rows)

    dataset_facts = (
        f"source_table = {profile.source_table or 'unknown'}\n"
        f"rows = {_fmt_num(profile.row_count)}\n"
        f"stats_available = {profile.stats_available}"
    )
    return {
        "dataset_facts": dataset_facts,
        "candidate_metrics": _lines([*profile.counts, *profile.measures], kind="metric"),
        "candidate_rates": _lines(profile.rates, kind="metric"),
        "candidate_dimensions": _lines(profile.dimensions, kind="field", role="categorical"),
        "candidate_dates": _lines(profile.dates, kind="field", role="date"),
        "derived_metrics": _lines(profile.derived_metrics, kind="metric"),
        "durations": _lines(profile.durations, kind="field", role="duration"),
        "identifier_fields": _lines(profile.identifiers, kind="field", role="identifier-like"),
    }


def widget_result_is_valid(widget: dict[str, Any]) -> bool:
    """Drop SQL errors, empty sets, and null-only KPI/chart results."""
    if not isinstance(widget, dict):
        return False
    wtype = str(widget.get("type") or "")
    if wtype in {"ai_insight", "text"}:
        return True
    if widget.get("error"):
        return False
    rows = widget.get("rows") or []
    if wtype == "kpi":
        value = widget.get("value")
        if value is None and rows and isinstance(rows[0], dict):
            value = rows[0].get("value")
            if value is None:
                value = next((v for v in rows[0].values() if v is not None), None)
        if value is None:
            return False
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return False
        return True
    if not rows:
        return False
    numeric_seen = False
    all_null = True
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, val in row.items():
            if val is None or val == "":
                continue
            all_null = False
            if key.lower() in {"value"} or isinstance(val, (int, float)):
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    continue
                numeric_seen = True
    if all_null:
        return False
    if wtype in {"chart", "pie"} and not numeric_seen:
        return False
    return True


def prune_invalid_widgets(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Remove failed/empty data widgets; keep insight/text widgets."""
    kept: list[dict[str, Any]] = []
    for widget in dashboard.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        if widget_result_is_valid(widget):
            kept.append(widget)
    out = dict(dashboard)
    out["widgets"] = kept
    return out
