"""Extract deterministic comparison facts from dual-period query results."""

from __future__ import annotations

from typing import Any

from app.agent.insight_validation import ComparisonDirectionFact
from app.agent.query_plan import QueryPlan


def _direction(current: float, previous: float) -> str:
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def extract_comparison_facts(
    rows: list[dict[str, Any]],
    *,
    query_plan: QueryPlan | None = None,
) -> list[ComparisonDirectionFact]:
    """Build comparison direction facts from standardized comparison SQL columns."""
    if not rows or not isinstance(rows[0], dict):
        return []

    row = rows[0]
    facts: list[ComparisonDirectionFact] = []

    comp = query_plan.comparison if query_plan else None
    if comp and comp.type == "entity_vs_entity" and comp.entities:
        entity_cols = [
            (k, v)
            for k, v in row.items()
            if str(k).startswith("entity_a_") or str(k).startswith("entity_b_")
        ]
        if len(entity_cols) >= 2:
            a_key, a_val = entity_cols[0]
            b_key, b_val = entity_cols[1]
            if isinstance(a_val, (int, float)) and isinstance(b_val, (int, float)):
                pct_key = next(
                    (k for k in row if str(k).startswith("percent_change_")),
                    None,
                )
                pct = row.get(pct_key) if pct_key else None
                facts.append(
                    ComparisonDirectionFact(
                        label=f"{comp.entities[0]} vs {comp.entities[1]}",
                        direction=_direction(float(a_val), float(b_val)),  # type: ignore[arg-type]
                        change_pct=float(pct) if isinstance(pct, (int, float)) else None,
                        left_label=comp.entities[0],
                        right_label=comp.entities[1],
                    )
                )
        return facts

    current_cols = [k for k in row if str(k).startswith("current_")]
    comparison_cols = [k for k in row if str(k).startswith("comparison_")]
    for cur_key in current_cols:
        suffix = str(cur_key)[len("current_") :]
        cmp_key = f"comparison_{suffix}"
        if cmp_key not in row:
            continue
        cur_val = row.get(cur_key)
        cmp_val = row.get(cmp_key)
        if not isinstance(cur_val, (int, float)) or not isinstance(cmp_val, (int, float)):
            continue
        pct_key = f"percent_change_{suffix}"
        pct = row.get(pct_key)
        label = suffix.replace("_", " ").title()
        if query_plan and query_plan.comparison:
            comp_type = query_plan.comparison.type.replace("_", " ")
            label = f"{label} ({comp_type})"
        facts.append(
            ComparisonDirectionFact(
                label=label,
                direction=_direction(float(cur_val), float(cmp_val)),
                change_pct=float(pct) if isinstance(pct, (int, float)) else None,
                left_label="current",
                right_label="comparison",
            )
        )
    return facts
