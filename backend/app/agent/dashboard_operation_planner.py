"""LLM planner for structured dashboard operations (no regex fallback)."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from app.agent.dashboard_operation_schema import (
    ClarificationRequest,
    DashboardOperationPlan,
    WidgetCatalogEntry,
)
from app.agent.prompts import get_prompt
from app.agent.sql_compiler import _catalog_maps


def _parse_plan_json(raw: str) -> DashboardOperationPlan | None:
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
    if not isinstance(data, dict):
        return None
    try:
        return DashboardOperationPlan.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def plan_dashboard_operations(
    llm: Any,
    instruction: str,
    *,
    dashboard_id: UUID,
    dashboard: dict[str, Any],
    catalog: list[WidgetCatalogEntry],
    semantic_catalog: dict[str, Any] | None,
) -> DashboardOperationPlan | None:
    """Plan dashboard edits via LLM structured JSON (no regex fallback)."""
    metrics, dimensions = _catalog_maps(semantic_catalog)
    metric_names = ", ".join(sorted(metrics.keys())) or "(none)"
    dim_names = ", ".join(sorted(dimensions.keys())) or "(none)"
    catalog_json = json.dumps(
        [c.model_dump(mode="json") for c in catalog],
        default=str,
    )[:6000]
    filters_json = json.dumps(dashboard.get("filters") or [], default=str)

    prompt = get_prompt("dashboard_operation_planner").render(
        dashboard_id=str(dashboard_id),
        semantic_metrics=metric_names,
        semantic_dimensions=dim_names,
        widget_catalog=catalog_json,
        dashboard_title=dashboard.get("title") or dashboard.get("name") or "Dashboard",
        dashboard_filters=filters_json,
        instruction=instruction,
    )

    raw = llm.complete(prompt)
    plan = _parse_plan_json(raw)
    if plan is not None:
        return plan

    repair = (
        f"{prompt}\n\nYour previous response was invalid JSON. "
        "Return ONLY valid DashboardOperationPlan JSON."
    )
    raw2 = llm.complete(repair)
    return _parse_plan_json(raw2)
