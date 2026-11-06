"""Structured query plans parsed from natural language."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.semantic_identity import match_catalog_names


class FilterSpec(BaseModel):
    field: str
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "like"] = "eq"
    value: str | int | float | list[str] | None = None


class TimeSpec(BaseModel):
    field: str | None = None
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None
    range: Literal[
        "today",
        "yesterday",
        "this_week",
        "last_week",
        "this_month",
        "last_month",
        "this_quarter",
        "last_quarter",
        "this_year",
        "last_year",
        "mtd",
        "last_7_days",
        "last_30_days",
        "ytd",
        "previous_period",
        "all",
    ] | None = None
    start: str | None = None
    end: str | None = None


class SortSpec(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "desc"


class ComparisonSpec(BaseModel):
    type: Literal[
        "previous_period",
        "previous_year",
        "previous_month",
        "previous_quarter",
        "entity_vs_entity",
        "share_of_total",
    ]
    entity_dimension: str | None = None
    entities: list[str] = Field(default_factory=list)
    include_absolute_change: bool = True
    include_percent_change: bool = True
    comparison_time: TimeSpec | None = None


class QueryPlan(BaseModel):
    intent: Literal["aggregation", "detail", "comparison", "investigation", "share_of_total"]
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    time: TimeSpec | None = None
    comparison: ComparisonSpec | None = None
    sort: list[SortSpec] = Field(default_factory=list)
    limit: int | None = None


def has_phrase(text: str, phrase: str) -> bool:
    """Whole-word match: "change" must not match "exchange rate", "drill"
    not "drilling", "mom" not "moment". Plain ``in`` checks misrouted real
    questions from other industries."""
    words = phrase.strip()
    if not words:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(words)}(?![a-z0-9])", text) is not None


_AGGREGATION_HINTS = (
    "total",
    "sum",
    "count",
    "average",
    "avg",
    "how many",
    "top ",
    "by ",
    "group",
    "breakdown",
    "per ",
)
_DETAIL_HINTS = ("list", "show me", "which rows", "details", "records for")
_COMPARISON_HINTS = ("compare", "vs", "versus", "difference", "growth", "change")
_SHARE_HINTS = ("share of total", "percentage of total", "percent of total", "% of total")
_INVESTIGATION_HINTS = ("why", "investigate", "drill", "anomaly", "outlier", "spike")

# "Show customer names along with their invoice numbers and values" names
# several raw per-record fields side by side — a row listing, not a summary
# number. The LLM plan parser sometimes misclassifies this shape as
# "aggregation" (then sql_compiler wraps whatever metric it picked in
# SUM(...), returning one unrelated number instead of a table). This is a
# deterministic backstop independent of what the LLM decided, matching the
# pattern of the other _looks_like_* heuristics in workflow.py.
_ROW_LISTING_PATTERNS = (
    r"\balong\s*with\b",
    r"\btogether\s+with\b",
    r"\balongside\b",
    r"\bwith\s+their\b",
    r"\band\s+their\b",
)


def _looks_like_row_listing(question: str) -> bool:
    text = (question or "").lower()
    if not any(re.search(p, text) for p in _ROW_LISTING_PATTERNS):
        return False
    # Explicit aggregation language still wins (e.g. "total sales along
    # with growth rate" is a comparison/aggregation ask, not a row list).
    if any(has_phrase(text, h) for h in _AGGREGATION_HINTS):
        return False
    return True


def _guess_intent(
    question: str,
) -> Literal["aggregation", "detail", "comparison", "investigation", "share_of_total"]:
    text = (question or "").lower()
    if any(has_phrase(text, h) for h in _INVESTIGATION_HINTS):
        return "investigation"
    if any(has_phrase(text, h) for h in _SHARE_HINTS):
        return "share_of_total"
    if any(has_phrase(text, h) for h in _COMPARISON_HINTS):
        return "comparison"
    if any(has_phrase(text, h) for h in _DETAIL_HINTS):
        return "detail"
    return "aggregation"


def _match_semantic_names(question: str, semantic_context: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    if not semantic_context:
        return [], []
    return (
        match_catalog_names(question, semantic_context.get("metrics")),
        match_catalog_names(question, semantic_context.get("dimensions")),
    )


def _parse_llm_plan(raw: str) -> QueryPlan | None:
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
        return QueryPlan.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def _heuristic_plan(
    question: str,
    semantic_context: dict[str, Any] | None,
) -> QueryPlan | None:
    text = (question or "").strip()
    if not text:
        return None

    metrics, dimensions = _match_semantic_names(text, semantic_context)
    intent = _guess_intent(text)
    limit: int | None = None
    if "top " in text.lower():
        match = re.search(r"top\s+(\d+)", text.lower())
        if match:
            limit = int(match.group(1))

    time_spec: TimeSpec | None = None
    lowered = text.lower()
    from app.agent.time_resolution import resolve_time_phrase

    time_spec = resolve_time_phrase(lowered)
    if time_spec is None and any(token in lowered for token in ("last quarter", "this quarter", "quarter")):
        time_spec = TimeSpec(range="last_quarter")

    if not metrics and semantic_context:
        metric_items = semantic_context.get("metrics") or []
        matched = _match_semantic_names(text, semantic_context)[0]
        if len(matched) == 1:
            metrics = matched

    if intent == "aggregation" and not dimensions:
        for item in semantic_context.get("dimensions") or [] if semantic_context else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name.lower() in lowered:
                dimensions.append(name)

    return QueryPlan(
        intent=intent,
        metrics=metrics,
        dimensions=dimensions,
        time=time_spec,
        limit=limit,
        sort=[SortSpec(field=metrics[0], direction="desc")] if metrics else [],
    )


def _apply_deterministic_overrides(plan: QueryPlan | None, question: str) -> QueryPlan | None:
    """Backstop the LLM/heuristic plan against known misclassification shapes."""
    if plan is None:
        return None
    if plan.intent != "detail" and _looks_like_row_listing(question):
        plan = plan.model_copy(deep=True)
        plan.intent = "detail"
        # These were chosen under a (wrong) aggregation assumption, so they
        # are unlikely to be the actual fields the user named — keep them
        # out of the select rather than aggregate or display an unrelated
        # column. compile_query_plan falls back to SELECT * when both
        # metrics and dimensions are empty.
        plan.metrics = []
    return plan


def parse_query_plan(
    llm_client: Any,
    question: str,
    semantic_context: dict[str, Any] | None,
    conversation_context: str | None = None,
) -> QueryPlan | None:
    """Parse a structured query plan from natural language."""
    if llm_client is not None and getattr(llm_client, "client", None) is not None:
        semantic_brief = json.dumps(semantic_context or {}, default=str)[:4000]
        prompt = (
            "Parse the user question into a JSON query plan with keys: "
            "intent (aggregation|detail|comparison|investigation), metrics, dimensions, "
            "filters (list of {field, operator, value}), time ({field, grain, range, start, end}), "
            "sort (list of {field, direction}), limit.\n\n"
            "Rules:\n"
            "- Use intent \"detail\" when the question asks to see raw per-record "
            "fields (e.g. \"show X along with Y and Z\", \"list ... with their ...\"), "
            "even if it also uses words like \"show\" or \"total\" elsewhere. Only use "
            "\"aggregation\" for an actual summary number (sum/count/average/top-N).\n"
            "- Only put a name in metrics or dimensions when it (or an unambiguous "
            "synonym) is explicitly named in the semantic context below AND clearly "
            "referenced by the question. Never substitute a different catalog metric "
            "just because it seems topically related — if nothing in the semantic "
            "context matches what the question asks for, leave metrics/dimensions "
            "empty rather than guess.\n\n"
            f"Semantic context:\n{semantic_brief}\n\n"
            f"Conversation context:\n{conversation_context or 'None'}\n\n"
            f"Question:\n{question}\n"
        )
        raw = llm_client.complete(prompt)
        plan = _parse_llm_plan(raw)
        if plan is not None:
            return _apply_deterministic_overrides(plan, question)

    return _apply_deterministic_overrides(_heuristic_plan(question, semantic_context), question)
