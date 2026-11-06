"""Rule-based follow-up resolution for analyst conversations."""

from __future__ import annotations

import re
from typing import Any

from app.agent.analysis_state import AnalysisState
from app.agent.query_plan import ComparisonSpec, FilterSpec, QueryPlan, TimeSpec, has_phrase


def is_follow_up_utterance(question: str, state: AnalysisState | None) -> bool:
    if state is None or state.query_plan is None:
        if state and state.last_sql:
            text = (question or "").strip().lower()
            words = text.split()
            if len(words) <= 12:
                referential = {
                    "this", "that", "these", "those", "it", "them", "their", "same", "only",
                }
                if any(w in words for w in referential):
                    return True
        return False

    text = (question or "").strip().lower()
    if not text:
        return False
    words = text.split()
    if len(words) > 14:
        return False

    referential = {
        "this", "that", "these", "those", "it", "them", "their", "same", "only", "just",
    }
    if any(w in words for w in referential):
        return True
    if len(words) <= 6:
        return True
    return False


def _match_clarification_choice(
    question: str,
    choices: list[dict[str, Any]],
) -> str | None:
    text = (question or "").strip()
    lowered = text.lower()
    num_match = re.match(r"^(\d+)$", text)
    if num_match:
        idx = int(num_match.group(1)) - 1
        if 0 <= idx < len(choices):
            label = choices[idx].get("label")
            return str(label) if label else None

    for choice in choices:
        if not isinstance(choice, dict):
            continue
        label = str(choice.get("label") or "").strip()
        if not label:
            continue
        label_lower = label.lower()
        if label_lower == lowered or label_lower in lowered:
            return label
    return None


def resolve_query_clarification(
    question: str,
    state: AnalysisState | None,
) -> QueryPlan | None:
    """Apply a user's clarification answer to the pending query plan."""
    if state is None or not state.pending_query_clarification:
        return None

    pending = state.pending_query_clarification
    kind = str(pending.get("kind") or "")
    choices = pending.get("choices") or []
    if not isinstance(choices, list) or not choices:
        return None

    chosen = _match_clarification_choice(question, choices)
    if not chosen:
        return None

    base = state.query_plan.model_copy(deep=True) if state.query_plan else QueryPlan(intent="aggregation")
    if kind == "pick_metric":
        base.metrics = [chosen]
    elif kind == "pick_date_field":
        if base.time is None:
            base.time = TimeSpec(field=chosen)
        else:
            base.time = base.time.model_copy(update={"field": chosen})
    elif kind == "pick_dimension":
        if base.comparison and base.comparison.type == "entity_vs_entity":
            base.comparison = base.comparison.model_copy(update={"entity_dimension": chosen})
        elif chosen not in base.dimensions:
            base.dimensions.append(chosen)
    else:
        return None

    return base


def _infer_filter_field(state: AnalysisState) -> str | None:
    plan = state.query_plan
    if plan and plan.dimensions:
        return plan.dimensions[0]
    if state.semantic_focus.dimensions:
        return state.semantic_focus.dimensions[0]
    return None


def resolve_follow_up(
    question: str,
    state: AnalysisState | None,
) -> tuple[QueryPlan | None, bool]:
    """Return merged query plan and whether follow-up rules handled the turn."""
    if state is None or state.query_plan is None:
        return None, False

    if not is_follow_up_utterance(question, state):
        return None, False

    text = (question or "").strip()
    lowered = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", text.lower())
    plan = state.query_plan.model_copy(deep=True)
    handled = False

    only_match = re.match(
        r"^(?:only|just|filter(?:\s+to|\s+by)?)\s+(.+)$",
        lowered,
        flags=re.IGNORECASE,
    )
    if only_match:
        value = text.split(maxsplit=1)[-1].strip().strip("\"'")
        field = _infer_filter_field(state)
        if field and value:
            plan.filters = [
                FilterSpec(field=field, operator="eq", value=value),
            ]
            handled = True

    region_match = re.search(
        r"(?:region|territory|area)\s*(?:=|:)?\s*([\"']?)([a-z0-9 _-]+)\1",
        lowered,
    )
    if region_match and not handled:
        field = _infer_filter_field(state) or "Region"
        plan.filters = [
            FilterSpec(field=field, operator="eq", value=region_match.group(2).strip().title()),
        ]
        handled = True

    if any(
        phrase in lowered
        for phrase in (
            "last year",
            "previous year",
            "year over year",
            "compare with last year",
            "compare to last year",
            "vs last year",
            "yoy",
        )
    ):
        plan.intent = "comparison"
        plan.time = TimeSpec(range="ytd")
        plan.comparison = ComparisonSpec(type="previous_year")
        handled = True

    if any(
        has_phrase(lowered, phrase)
        for phrase in (
            "compare with last month",
            "compare to last month",
            "previous month",
            "mom",
            "month over month",
        )
    ) and "last year" not in lowered:
        plan.intent = "comparison"
        plan.time = TimeSpec(range="this_month")
        plan.comparison = ComparisonSpec(type="previous_month")
        handled = True

    if any(phrase in lowered for phrase in ("previous period", "prior period")) and not handled:
        plan.intent = "comparison"
        if plan.time is None:
            plan.time = TimeSpec(range="this_month")
        plan.comparison = ComparisonSpec(type="previous_period")
        handled = True

    if any(phrase in lowered for phrase in ("last quarter", "previous quarter")) and not handled:
        plan.time = TimeSpec(range="last_quarter")
        handled = True

    if any(phrase in lowered for phrase in ("last month", "previous month", "prior month")) and not handled:
        plan.time = TimeSpec(range="last_month")
        handled = True

    if any(phrase in lowered for phrase in ("last 30 days", "past month")) and not handled:
        plan.time = TimeSpec(range="last_30_days")
        handled = True

    if any(phrase in lowered for phrase in ("last 7 days", "last week", "past week")) and not handled:
        plan.time = TimeSpec(range="last_7_days")
        handled = True

    if ("ytd" in lowered or "year to date" in lowered) and "last year" not in lowered and not handled:
        plan.time = TimeSpec(range="ytd")
        handled = True

    top_match = re.search(r"top\s+(\d+)", lowered)
    if top_match and not handled:
        plan.limit = int(top_match.group(1))
        if plan.metrics:
            from app.agent.query_plan import SortSpec

            plan.sort = [SortSpec(field=plan.metrics[0], direction="desc")]
        handled = True

    bottom_match = re.search(r"bottom\s+(\d+)", lowered)
    if bottom_match and not handled:
        plan.limit = int(bottom_match.group(1))
        if plan.metrics:
            from app.agent.query_plan import SortSpec

            plan.sort = [SortSpec(field=plan.metrics[0], direction="asc")]
        handled = True

    if re.search(r"\bby\s+(day|week|month|quarter|year)\b", lowered) and not handled:
        grain_match = re.search(r"\bby\s+(day|week|month|quarter|year)\b", lowered)
        if grain_match:
            from app.agent.query_plan import SortSpec

            grain = grain_match.group(1)
            if plan.time is None:
                plan.time = TimeSpec(grain=grain)  # type: ignore[arg-type]
            else:
                plan.time = plan.time.model_copy(update={"grain": grain})
            handled = True

    if handled:
        return plan, True
    return None, False


def convo_dict_from_state(state: AnalysisState | None) -> dict[str, Any]:
    """Legacy convo shape for heuristic helpers during transition."""
    if state is None:
        return {}
    blob = {
        "last_sql": state.last_sql,
        "messages": [],
        "dashboard": None,
    }
    if state.last_user_question:
        blob["messages"].append({"role": "user", "content": state.last_user_question})
    if state.dashboard.dashboard_id:
        blob["dashboard"] = {
            "id": str(state.dashboard.dashboard_id),
            "title": state.dashboard.title,
            "widgets": [{"id": state.dashboard.active_widget_id or "w1"}],
        }
    return blob
