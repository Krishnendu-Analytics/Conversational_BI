"""Build dashboard widgets from tenant semantic catalog (no column guessing)."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from app.agent.dashboard_operation_schema import WidgetAddSpec
from app.agent.query_plan import QueryPlan
from app.agent.sql_compiler import _catalog_maps, compile_query_plan


def _canonical_metric_name(name: str, metrics: dict[str, str]) -> str:
    key = name.lower()
    for catalog_name in metrics:
        if catalog_name.lower() == key:
            return catalog_name
    raise ValueError(f"Unknown metric: {name}")


def _canonical_dimension_name(name: str, dimensions: dict[str, str]) -> str:
    key = name.lower()
    for catalog_name in dimensions:
        if catalog_name.lower() == key:
            return catalog_name
    raise ValueError(f"Unknown dimension: {name}")


def _resolve_metric(spec: WidgetAddSpec, metrics: dict[str, str]) -> str | None:
    if not spec.metric:
        return None
    return _canonical_metric_name(spec.metric, metrics)


def _resolve_dimension(spec: WidgetAddSpec, dimensions: dict[str, str]) -> str | None:
    if not spec.dimension:
        return None
    return _canonical_dimension_name(spec.dimension, dimensions)


def build_widget_from_semantic(
    spec: WidgetAddSpec,
    *,
    semantic_catalog: dict[str, Any],
    schema: dict[str, Any],
    db_type: str,
) -> dict[str, Any]:
    """Create a widget dict with SQL compiled from semantic catalog only."""
    metrics, dimensions = _catalog_maps(semantic_catalog)
    if not metrics and spec.widget_type == "kpi":
        raise ValueError("No semantic metrics configured")

    metric_name = _resolve_metric(spec, metrics) if spec.metric else None
    dimension_name = _resolve_dimension(spec, dimensions) if spec.dimension else None

    if spec.widget_type == "ai_insight":
        return {
            "id": f"insight-{uuid4().hex[:8]}",
            "type": "ai_insight",
            "title": spec.title or "AI Insights",
            "chart": None,
            "sql": None,
            "rows": [],
        }

    if spec.widget_type == "text":
        return {
            "id": f"text-{uuid4().hex[:8]}",
            "type": "text",
            "title": spec.title or "Note",
            "content": spec.title or "",
            "sql": None,
            "rows": [],
        }

    if spec.widget_type == "kpi":
        if not metric_name:
            if len(metrics) == 1:
                metric_name = next(iter(metrics.keys()))
            else:
                raise ValueError("Metric is required for KPI widgets")
        plan = QueryPlan(intent="aggregation", metrics=[metric_name])
        sql = compile_query_plan(plan, semantic_catalog, schema, db_type)
        title = spec.title or metric_name.replace("_", " ").title()
        return {
            "id": f"kpi-{uuid4().hex[:8]}",
            "type": "kpi",
            "title": title,
            "sql": sql,
            "chart": None,
            "rows": [],
        }

    chart_type = spec.chart or ("line" if spec.role == "trend" else "bar")
    if not metric_name:
        raise ValueError("Metric is required for chart widgets")

    plan_metrics = [metric_name]
    plan_dims: list[str] = []
    if dimension_name:
        plan_dims = [dimension_name]

    plan = QueryPlan(
        intent="aggregation",
        metrics=plan_metrics,
        dimensions=plan_dims,
        limit=24 if chart_type == "line" else 15,
    )
    sql = compile_query_plan(plan, semantic_catalog, schema, db_type)
    title = spec.title
    if not title:
        if dimension_name:
            title = f"{metric_name} by {dimension_name}"
        else:
            title = metric_name.replace("_", " ").title()

    return {
        "id": f"chart-{uuid4().hex[:8]}",
        "type": "chart",
        "title": title,
        "sql": sql,
        "chart": chart_type,
        "rows": [],
    }


def regenerate_widget_sql(
    widget: dict[str, Any],
    patch_metric: str | None,
    patch_dimension: str | None,
    *,
    semantic_catalog: dict[str, Any],
    schema: dict[str, Any],
    db_type: str,
) -> str:
    """Recompile widget SQL when metric/dimension changes on update."""
    metrics, dimensions = _catalog_maps(semantic_catalog)
    wtype = widget.get("type")
    if wtype == "kpi":
        metric = _canonical_metric_name(patch_metric or "", metrics) if patch_metric else None
        if not metric and widget.get("title"):
            for name in metrics:
                if name.lower() in str(widget["title"]).lower():
                    metric = name
                    break
        if not metric:
            raise ValueError("Cannot determine metric for widget SQL regeneration")
        plan = QueryPlan(intent="aggregation", metrics=[metric])
    elif wtype == "chart":
        metric = None
        if patch_metric:
            metric = _canonical_metric_name(patch_metric, metrics)
        dim = None
        if patch_dimension:
            dim = _canonical_dimension_name(patch_dimension, dimensions)
        if not metric:
            raise ValueError("Metric is required to regenerate chart SQL")
        plan = QueryPlan(
            intent="aggregation",
            metrics=[metric],
            dimensions=[dim] if dim else [],
            limit=24 if widget.get("chart") == "line" else 15,
        )
    else:
        raise ValueError(f"Cannot regenerate SQL for widget type {wtype!r}")

    return compile_query_plan(plan, semantic_catalog, schema, db_type)
