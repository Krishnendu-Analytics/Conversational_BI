"""Centralized calendar time phrase resolution and previous-period derivation."""

from __future__ import annotations

import re

from datetime import date
from typing import Literal

from app.agent.query_plan import TimeSpec, has_phrase

# Injectable reference date for deterministic tests (defaults to DB CURRENT_DATE in SQL).
_reference_date: date | None = None


def set_reference_date(value: date | None) -> None:
    """Override reference date for tests only."""
    global _reference_date
    _reference_date = value


def get_reference_date() -> date:
    return _reference_date or date.today()


TimeRange = Literal[
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
    "ytd",
    "last_7_days",
    "last_30_days",
    "previous_period",
    "all",
]


def resolve_time_phrase(text: str) -> TimeSpec | None:
    """Map natural-language time phrases to explicit TimeSpec ranges."""
    # "year-over-year" / "month-on-month" read the same as the spaced forms.
    lowered = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", (text or "").lower())

    if "previous period" in lowered or "prior period" in lowered:
        return TimeSpec(range="previous_period")

    if any(p in lowered for p in ("month to date", "month-to-date", "mtd")):
        if "last month" not in lowered:
            return TimeSpec(range="mtd")

    if any(p in lowered for p in ("year to date", "year-to-date", "ytd")):
        if "last year" not in lowered:
            return TimeSpec(range="ytd")

    if any(p in lowered for p in ("last year", "previous year", "prior year")):
        return TimeSpec(range="last_year")

    if any(p in lowered for p in ("this year", "current year")) and "last year" not in lowered:
        return TimeSpec(range="this_year")

    if any(p in lowered for p in ("last month", "previous month", "prior month")):
        return TimeSpec(range="last_month")

    if any(p in lowered for p in ("this month", "current month")) and "last month" not in lowered:
        return TimeSpec(range="this_month")

    if any(p in lowered for p in ("last quarter", "previous quarter", "prior quarter")):
        return TimeSpec(range="last_quarter")

    if any(p in lowered for p in ("this quarter", "current quarter")) and "last quarter" not in lowered:
        return TimeSpec(range="this_quarter")

    if any(p in lowered for p in ("last week", "previous week", "past week")) and "this week" not in lowered:
        return TimeSpec(range="last_week")

    if any(p in lowered for p in ("this week", "current week")):
        return TimeSpec(range="this_week")

    if "yesterday" in lowered:
        return TimeSpec(range="yesterday")

    if re_today(lowered):
        return TimeSpec(range="today")

    if any(p in lowered for p in ("last 30 days", "past 30 days")):
        return TimeSpec(range="last_30_days")

    if any(p in lowered for p in ("last 7 days", "past 7 days")):
        return TimeSpec(range="last_7_days")

    return None


def re_today(lowered: str) -> bool:
    return (
        " today" in f" {lowered}"
        or lowered.startswith("today")
        or lowered.endswith(" today")
    )


def _has_explicit_comparison_cue(lowered: str) -> bool:
    """True when the user asked to compare periods, not merely filter to one period."""
    padded = f" {lowered} "
    if any(
        has_phrase(lowered, cue)
        for cue in (
            "compare",
            "compared",
            "versus",
            "yoy",
            "mom",
            "qoq",
            "year over year",
            "month over month",
            "quarter over quarter",
        )
    ):
        return True
    if " vs " in padded or padded.startswith("vs ") or padded.endswith(" vs"):
        return True
    if "this quarter" in lowered and "last quarter" in lowered:
        return True
    if "this year" in lowered and "last year" in lowered:
        return True
    if "this month" in lowered and "last month" in lowered:
        return True
    return False


def infer_comparison_type(text: str, primary: TimeSpec | None) -> str | None:
    """Infer comparison.type from question when the user asked to compare periods.

    A lone time-range phrase ("last quarter") is a filter, not a comparison.
    Dual-period comparisons require an explicit cue (compare/vs/qoq, or this+last).
    """
    # "year-over-year" / "month-on-month" read the same as the spaced forms.
    lowered = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", (text or "").lower())
    if any(p in lowered for p in ("share of total", "percentage of total", "percent of total", "% of total")):
        return "share_of_total"
    if any(p in lowered for p in ("previous period", "prior period")):
        return "previous_period"

    explicit = _has_explicit_comparison_cue(lowered)

    if has_phrase(lowered, "yoy") or "year over year" in lowered:
        return "previous_year"
    if has_phrase(lowered, "mom") or "month over month" in lowered:
        return "previous_month"
    if has_phrase(lowered, "qoq") or "quarter over quarter" in lowered:
        return "previous_quarter"

    if explicit:
        if any(p in lowered for p in ("last year", "previous year", "prior year")):
            return "previous_year"
        if any(p in lowered for p in ("last month", "previous month", "prior month")):
            return "previous_month"
        if any(p in lowered for p in ("last quarter", "previous quarter")):
            return "previous_quarter"

    if primary and primary.range == "previous_period":
        return "previous_period"
    if primary and primary.range in {"ytd", "this_year"}:
        if "last year" in lowered or "previous year" in lowered:
            return "previous_year"
    if primary and primary.range in {"this_month", "mtd"}:
        if "last month" in lowered or "previous month" in lowered:
            return "previous_month"
    return None


def derive_comparison_period(primary: TimeSpec) -> TimeSpec | None:
    """Derive the comparison period from the primary period/granularity."""
    field = primary.field
    grain = primary.grain

    if primary.range == "previous_period":
        return None

    if primary.range in {"this_month", "mtd"}:
        return TimeSpec(field=field, grain=grain, range="last_month")
    if primary.range in {"this_year", "ytd"}:
        return TimeSpec(field=field, grain=grain, range="last_year")
    if primary.range == "this_quarter":
        return TimeSpec(field=field, grain=grain, range="last_quarter")
    if primary.range == "this_week":
        return TimeSpec(field=field, grain=grain, range="last_week")
    if primary.range == "today":
        return TimeSpec(field=field, grain=grain, range="yesterday")
    if primary.range in {"last_month", "last_quarter", "last_year", "last_week", "last_7_days", "last_30_days"}:
        return TimeSpec(field=field, grain=grain, range="previous_period")

    if grain:
        grain_map = {
            "day": "yesterday",
            "week": "last_week",
            "month": "last_month",
            "quarter": "last_quarter",
            "year": "last_year",
        }
        comp = grain_map.get(grain)
        if comp:
            return TimeSpec(field=field, grain=grain, range=comp)  # type: ignore[arg-type]

    return None


def primary_period_for_comparison_type(comp_type: str, field: str | None = None) -> TimeSpec:
    """Default primary period when comparison type is explicit."""
    defaults: dict[str, TimeSpec] = {
        "previous_year": TimeSpec(field=field, range="ytd"),
        "previous_month": TimeSpec(field=field, range="this_month"),
        "previous_quarter": TimeSpec(field=field, range="this_quarter"),
        "previous_period": TimeSpec(field=field, range="this_month"),
    }
    return defaults.get(comp_type, TimeSpec(field=field, range="ytd"))
