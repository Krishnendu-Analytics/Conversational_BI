"""Semantic widget target resolution for dashboard operations."""

from __future__ import annotations

import re

from app.agent.dashboard_operation_schema import (
    WidgetCatalogEntry,
    WidgetRef,
    WidgetResolution,
)

_SCORE_TIE_THRESHOLD = 3.0
_AMBIGUITY_GAP = 10.0


def _normalize_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 1
    }


def _score_target(
    target: WidgetRef,
    entry: WidgetCatalogEntry,
    *,
    active_widget_id: str | None,
    apply_tiebreak: bool,
) -> float:
    if target.widget_id and target.widget_id == entry.id:
        return 1000.0

    score = 0.0
    title_tokens = _normalize_tokens(target.title or "")
    entry_tokens = _normalize_tokens(entry.title)

    if title_tokens and entry_tokens:
        overlap = title_tokens & entry_tokens
        score += min(40.0, len(overlap) * 12.0)

    if target.metric:
        metric_lower = target.metric.lower()
        for m in entry.metrics:
            if m.lower() == metric_lower or metric_lower in m.lower():
                score += 35.0
                break

    if target.dimension:
        dim_lower = target.dimension.lower()
        for d in entry.dimensions:
            if d.lower() == dim_lower or dim_lower in d.lower():
                score += 25.0
                break

    if target.widget_type and entry.type == target.widget_type:
        score += 20.0

    if target.chart and entry.chart == target.chart:
        score += 15.0

    if target.role == "trend" and entry.chart == "line":
        score += 15.0
    if target.role == "breakdown" and entry.chart == "bar":
        score += 15.0
    if target.role == "kpi" and entry.type == "kpi":
        score += 15.0
    if target.role == "insight" and entry.type == "ai_insight":
        score += 15.0

    if apply_tiebreak and active_widget_id and entry.id == active_widget_id:
        score += 5.0

    return score


def _resolve_ordinal_target(
    target: WidgetRef,
    catalog: list[WidgetCatalogEntry],
) -> WidgetResolution | None:
    """Positional match ("the second chart", "the last kpi") — one widget,
    never the whole-type bulk match, counted in catalog/display order."""
    if target.ordinal is None or target.ordinal == 0:
        return None
    candidates = (
        [e for e in catalog if e.type == target.widget_type]
        if target.widget_type
        else list(catalog)
    )
    if not candidates:
        return WidgetResolution(status="not_found")
    index = target.ordinal - 1 if target.ordinal > 0 else target.ordinal
    if not (-len(candidates) <= index < len(candidates)):
        return WidgetResolution(status="not_found")
    return WidgetResolution(status="resolved", widget_ids=[candidates[index].id])


def resolve_widget_target(
    target: WidgetRef | None,
    catalog: list[WidgetCatalogEntry],
    *,
    active_widget_id: str | None = None,
    allow_bulk_type: bool = False,
) -> WidgetResolution:
    if target is None:
        return WidgetResolution(status="not_found")

    if target.widget_id:
        matches = [e for e in catalog if e.id == target.widget_id]
        if matches:
            return WidgetResolution(status="resolved", widget_ids=[target.widget_id])
        return WidgetResolution(status="not_found")

    ordinal_result = _resolve_ordinal_target(target, catalog)
    if ordinal_result is not None:
        return ordinal_result

    if allow_bulk_type and target.widget_type and not (
        target.title or target.metric or target.dimension
    ):
        ids = [e.id for e in catalog if e.type == target.widget_type]
        if ids:
            return WidgetResolution(status="resolved", widget_ids=ids)
        return WidgetResolution(status="not_found")

    if not catalog:
        return WidgetResolution(status="not_found")

    scored: list[tuple[float, WidgetCatalogEntry]] = []
    for entry in catalog:
        s = _score_target(target, entry, active_widget_id=active_widget_id, apply_tiebreak=False)
        if s > 0:
            scored.append((s, entry))

    if not scored:
        return WidgetResolution(status="not_found")

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score = scored[0][0]

    tied_top = [e for s, e in scored if top_score - s <= _SCORE_TIE_THRESHOLD]
    if len(tied_top) > 1 and active_widget_id:
        rescored = [
            (
                _score_target(
                    target,
                    e,
                    active_widget_id=active_widget_id,
                    apply_tiebreak=True,
                ),
                e,
            )
            for e in tied_top
        ]
        rescored.sort(key=lambda x: x[0], reverse=True)
        top_score = rescored[0][0]
        scored = rescored + [(s, e) for s, e in scored if e not in tied_top]

    top_entries = [e for s, e in scored if top_score - s <= _AMBIGUITY_GAP]

    if len(top_entries) == 1:
        return WidgetResolution(
            status="resolved",
            widget_ids=[top_entries[0].id],
            scores=[top_score],
        )

    if len(top_entries) > 1 and top_score - scored[1][0] <= _AMBIGUITY_GAP:
        return WidgetResolution(
            status="ambiguous",
            candidates=top_entries[:5],
            scores=[s for s, _ in scored[: len(top_entries)]],
        )

    return WidgetResolution(
        status="resolved",
        widget_ids=[scored[0][1].id],
        scores=[top_score],
    )
