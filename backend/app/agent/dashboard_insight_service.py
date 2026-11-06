"""Orchestrate AI insight widget refresh after dashboard execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.agent.dashboard_insight_facts import (
    DashboardInsightFacts,
    extract_dashboard_insight_facts,
    facts_fingerprint,
    render_deterministic_insight_markdown,
)
from app.agent.dashboard_insight_narrator import narrate_dashboard_insights

_INSIGHT_CONFIG_KEYS = (
    "content",
    "insight_status",
    "insight_facts",
    "facts_hash",
    "generated_at",
    "narrator",
    "error",
)


def _widget_config(widget: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(widget.get("config") or {})
    for key in _INSIGHT_CONFIG_KEYS:
        if widget.get(key) is not None:
            cfg[key] = widget[key]
    return cfg


def _apply_insight_config(widget: dict[str, Any], config: dict[str, Any]) -> None:
    widget["config"] = config
    widget["content"] = config.get("content")
    widget["insight_status"] = config.get("insight_status")
    widget["insight_generated_at"] = config.get("generated_at")


def ensure_ai_insight_widget(dashboard: dict[str, Any]) -> dict[str, Any]:
    """Append a default AI Insights widget when missing."""
    widgets = list(dashboard.get("widgets") or [])
    if any(str(w.get("type") or "") == "ai_insight" for w in widgets if isinstance(w, dict)):
        return dashboard

    widgets.append(
        {
            "id": f"insight-{uuid4().hex[:8]}",
            "type": "ai_insight",
            "title": "AI Insights",
            "sql": None,
            "rows": [],
        }
    )
    dashboard["widgets"] = widgets
    return dashboard


def _build_insight_config(
    facts: DashboardInsightFacts,
    *,
    content: str,
    narrator: str,
    facts_hash: str,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "content": content,
        "insight_status": facts.status.value,
        "insight_facts": facts.model_dump(mode="json"),
        "facts_hash": facts_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "narrator": narrator,
        "error": error,
    }


def refresh_ai_insight_widgets(
    dashboard: dict[str, Any],
    *,
    semantic: dict[str, Any] | None = None,
    llm: Any | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh all ai_insight widgets from executed sibling widget data."""
    facts = extract_dashboard_insight_facts(dashboard, semantic=semantic)
    fp = facts_fingerprint(facts)

    for widget in dashboard.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        if str(widget.get("type") or "") != "ai_insight":
            continue

        config = _widget_config(widget)
        cached_hash = str(config.get("facts_hash") or "")
        cached_status = str(config.get("insight_status") or "")

        if (
            not force
            and cached_hash == fp
            and cached_status == facts.status.value
            and config.get("content")
        ):
            _apply_insight_config(widget, config)
            continue

        if facts.status.value == "insufficient_data":
            content = render_deterministic_insight_markdown(facts)
            narrator = "deterministic_fallback"
        else:
            content, narrator = narrate_dashboard_insights(facts, llm)

        new_config = _build_insight_config(
            facts,
            content=content,
            narrator=narrator,
            facts_hash=fp,
        )
        _apply_insight_config(widget, new_config)

    return dashboard


def merge_insight_config_into_widget_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy insight fields from widget dict into config for persistence."""
    config = dict(raw.get("config") or {})
    if raw.get("chart") is not None:
        config["chart"] = raw.get("chart")
    if raw.get("rows"):
        config["rows"] = raw.get("rows")
    if raw.get("value") is not None:
        config["value"] = raw.get("value")
    if raw.get("error"):
        config["error"] = raw.get("error")
    for key in (
        "result",
        "value_kind",
        "aggregation",
        "metric",
        "dimension",
        "purpose",
        "metric_expression",
        "quality",
        "section",
        "polarity",
        "measure_labels",
        "unit",
        "note",
        "domain",
        "data_warning",
        "period_kind",
        # "document": rows extracted from an attached file, not a query.
        "source",
    ):
        if raw.get(key) is not None:
            config[key] = raw[key]
    for key in _INSIGHT_CONFIG_KEYS:
        if raw.get(key) is not None:
            config[key] = raw[key]
    return config
