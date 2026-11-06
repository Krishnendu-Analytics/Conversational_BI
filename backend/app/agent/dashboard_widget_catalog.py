"""Build semantic widget catalog from dashboard spec + tenant catalog."""

from __future__ import annotations

import re
from typing import Any

from app.agent.dashboard_operation_schema import WidgetCatalogEntry
from app.agent.sql_compiler import _catalog_maps


def _normalize_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 1
    }


def _infer_semantic_names(
    sql: str,
    title: str,
    metrics: dict[str, str],
    dimensions: dict[str, str],
) -> tuple[list[str], list[str]]:
    sql_lower = (sql or "").lower()
    title_tokens = _normalize_tokens(title)
    found_metrics: list[str] = []
    found_dims: list[str] = []

    for name, expr in metrics.items():
        expr_lower = expr.lower()
        if expr_lower in sql_lower or name in title_tokens:
            found_metrics.append(name.title() if name.islower() else name)

    for name, expr in dimensions.items():
        expr_lower = expr.lower()
        if expr_lower in sql_lower or name in title_tokens:
            found_dims.append(name.title() if name.islower() else name)

    if not found_metrics:
        for token in title_tokens:
            if token in metrics:
                found_metrics.append(token.title())

    return list(dict.fromkeys(found_metrics)), list(dict.fromkeys(found_dims))


def build_widget_catalog(
    dashboard: dict[str, Any],
    semantic_catalog: dict[str, Any] | None,
) -> list[WidgetCatalogEntry]:
    metrics, dimensions = _catalog_maps(semantic_catalog)
    entries: list[WidgetCatalogEntry] = []

    for raw in dashboard.get("widgets") or []:
        if not isinstance(raw, dict):
            continue
        wid = str(raw.get("id") or "")
        if not wid:
            continue
        title = str(raw.get("title") or wid)
        sql = str(raw.get("sql") or "")
        w_metrics, w_dims = _infer_semantic_names(sql, title, metrics, dimensions)
        entries.append(
            WidgetCatalogEntry(
                id=wid,
                type=str(raw.get("type") or "chart"),
                title=title,
                chart=raw.get("chart"),
                metrics=w_metrics,
                dimensions=w_dims,
                position=dict(raw.get("position") or {}),
                has_error=bool(raw.get("error")),
            )
        )
    return entries


def catalog_to_dicts(entries: list[WidgetCatalogEntry]) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in entries]
