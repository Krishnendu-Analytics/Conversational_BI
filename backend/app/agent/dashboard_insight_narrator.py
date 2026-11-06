"""LLM narrator for dashboard AI insight widgets (facts-only input)."""

from __future__ import annotations

import json
from typing import Any

from app.agent.dashboard_insight_facts import (
    DashboardInsightFacts,
    InsightFactStatus,
    render_deterministic_insight_markdown,
    validate_narrative_numbers,
)
from app.agent.prompts import get_prompt

_MAX_FACTS_JSON = 8192


def narrate_dashboard_insights(
    facts: DashboardInsightFacts,
    llm: Any | None,
) -> tuple[str, str]:
    """Return (markdown content, narrator mode: llm | deterministic_fallback)."""
    if facts.status == InsightFactStatus.INSUFFICIENT_DATA:
        return render_deterministic_insight_markdown(facts), "deterministic_fallback"

    fallback = render_deterministic_insight_markdown(facts)
    if llm is None:
        return fallback, "deterministic_fallback"

    facts_json = json.dumps(
        facts.model_dump(mode="json"),
        default=str,
    )[:_MAX_FACTS_JSON]

    prompt = get_prompt("dashboard_insight_narrator").render(facts_json=facts_json)
    try:
        raw = llm.complete(prompt)
    except Exception:  # noqa: BLE001
        return fallback, "deterministic_fallback"

    if not raw or str(raw).strip().lower().startswith("[llm stub]"):
        return fallback, "deterministic_fallback"

    content = str(raw).strip()
    if content.startswith("```"):
        lines = content.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        content = "\n".join(body).strip()

    if not validate_narrative_numbers(content, facts):
        return fallback, "deterministic_fallback"

    return content, "llm"
