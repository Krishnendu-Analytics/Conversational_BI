"""Deterministic dashboard insight facts — sole source of numeric truth."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from enum import Enum
from statistics import mean, pstdev
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.measure_kind import NON_ADDITIVE_KINDS


class InsightFactStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    PARTIAL_DATA = "partial_data"


class KpiFact(BaseModel):
    widget_id: str
    title: str
    value: float | None = None
    formatted_value: str = "—"
    metric_hint: str | None = None
    value_kind: str = "amount"
    aggregation: str | None = None
    significance_score: float = 0.0


class CategoryEntry(BaseModel):
    label: str
    value: float
    share_pct: float | None = None


class CategoryFact(BaseModel):
    widget_id: str
    title: str
    chart: str | None = None
    top: list[CategoryEntry] = Field(default_factory=list)
    bottom: list[CategoryEntry] = Field(default_factory=list)
    total: float | None = None
    concentration_top1_pct: float | None = None
    value_kind: str = "amount"
    aggregation: str | None = None
    dimension: str | None = None
    category_count: int = 0
    meaningful: bool = True
    significance_score: float = 0.0


class AnomalyPoint(BaseModel):
    period: str
    value: float
    z_score: float


class TrendFact(BaseModel):
    widget_id: str
    title: str
    points: int = 0
    first_value: float | None = None
    last_value: float | None = None
    change_abs: float | None = None
    change_pct: float | None = None
    direction: Literal["up", "down", "flat", "unknown"] = "unknown"
    potential_anomalies: list[AnomalyPoint] = Field(default_factory=list)
    significance_score: float = 0.0


class ComparisonFact(BaseModel):
    label: str
    left: str
    right: str
    ratio: float | None = None
    rate_pct: float | None = None
    significance_score: float = 0.0


class WidgetErrorFact(BaseModel):
    widget_id: str
    title: str
    error: str


class DashboardInsightFacts(BaseModel):
    version: int = 1
    dashboard_id: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: InsightFactStatus = InsightFactStatus.INSUFFICIENT_DATA
    insufficient_reason: str | None = None
    active_filters: list[dict[str, Any]] = Field(default_factory=list)
    kpis: list[KpiFact] = Field(default_factory=list)
    categories: list[CategoryFact] = Field(default_factory=list)
    trends: list[TrendFact] = Field(default_factory=list)
    comparisons: list[ComparisonFact] = Field(default_factory=list)
    widget_errors: list[WidgetErrorFact] = Field(default_factory=list)
    source_widget_ids: list[str] = Field(default_factory=list)
    ranked_highlights: list[str] = Field(default_factory=list)


# Percentages, rates, averages, min/max and running totals must never be
# summed or shown as a share of a series total.
_NON_ADDITIVE_KINDS = NON_ADDITIVE_KINDS | {"rate"}


def _as_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            n = float(cleaned)
            return n if math.isfinite(n) else None
        except ValueError:
            return None
    return None


def _format_number(n: float) -> str:
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs_n >= 10_000:
        return f"{n:,.0f}"
    if float(n).is_integer():
        return f"{int(n):,}"
    return f"{n:,.2f}"


_MONTH_NAMES = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
]


def _period_sort_key(label: str) -> tuple[int, int, int] | None:
    """Comparable key for common period labels: 2025-10(-01...), 'October
    2025' / 'Oct 2025', '2025 Q3', '2025'. None when the label isn't one."""
    text = str(label or "").strip().lower()
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    m = re.match(r"^([a-z]+)\.?\s+(\d{4})$", text)
    if m and m.group(1)[:3] in _MONTH_NAMES:
        return int(m.group(2)), _MONTH_NAMES.index(m.group(1)[:3]) + 1, 0
    m = re.match(r"^(\d{4})\s*q([1-4])$", text)
    if m:
        return int(m.group(1)), int(m.group(2)) * 3, 0
    if re.fullmatch(r"\d{4}", text):
        return int(text), 0, 0
    return None


def _chronological(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Rows in time order: sorted by period when every label is a period,
    otherwise the query's own row order."""
    keys = [_period_sort_key(label) for label, _ in pairs]
    if all(k is not None for k in keys):
        return [p for _, p in sorted(zip(keys, pairs), key=lambda kp: kp[0])]
    return list(pairs)


def _is_source_widget(widget: dict[str, Any]) -> bool:
    wtype = str(widget.get("type") or "")
    # A document's raw line-item table is not a category breakdown: its first
    # numeric column is often a serial or lot number, not a measure.
    if wtype == "table" and widget.get("source") == "document":
        return False
    return wtype not in {"ai_insight", "text"}


def _parse_category_rows(rows: list[dict[str, Any]]) -> list[tuple[str, float]]:
    if not rows:
        return []
    first = rows[0]
    if not isinstance(first, dict):
        return []

    label_key = None
    value_key = None
    for key in ("category", "period", "label", "name"):
        if key in first:
            label_key = key
            break
    for key in ("value", "amount", "total", "count"):
        if key in first:
            value_key = key
            break

    if label_key and value_key:
        pairs: list[tuple[str, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get(label_key) or "").strip()
            val = _as_number(row.get(value_key))
            if label and val is not None:
                pairs.append((label, val))
        return pairs

    keys = list(first.keys())
    text_keys = [k for k in keys if _as_number(first.get(k)) is None]
    num_keys = [k for k in keys if _as_number(first.get(k)) is not None]
    if not text_keys or not num_keys:
        return []
    lk, vk = text_keys[0], num_keys[0]
    pairs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get(lk) or "").strip()
        val = _as_number(row.get(vk))
        if label and val is not None:
            pairs.append((label, val))
    return pairs


def _zscore_anomalies(pairs: list[tuple[str, float]]) -> list[AnomalyPoint]:
    if len(pairs) < 8:
        return []
    values = [v for _, v in pairs]
    if len(set(values)) <= 1:
        return []
    mu = mean(values)
    sigma = pstdev(values)
    if sigma == 0:
        return []
    out: list[AnomalyPoint] = []
    for label, val in pairs:
        z = (val - mu) / sigma
        if abs(z) >= 2.0:
            out.append(AnomalyPoint(period=label, value=val, z_score=round(z, 2)))
    out.sort(key=lambda a: abs(a.z_score), reverse=True)
    return out[:3]


def _trend_direction(change_pct: float | None) -> Literal["up", "down", "flat", "unknown"]:
    if change_pct is None:
        return "unknown"
    if abs(change_pct) < 1.0:
        return "flat"
    return "up" if change_pct > 0 else "down"


def _score_trend(change_pct: float | None, anomalies: list[AnomalyPoint]) -> float:
    score = 0.0
    if change_pct is not None:
        score += min(50.0, abs(change_pct))
    if anomalies:
        score += min(30.0, max(abs(a.z_score) for a in anomalies) * 10.0)
    return score


def _score_concentration(top1_pct: float | None) -> float:
    if top1_pct is None:
        return 0.0
    return min(40.0, max(0.0, top1_pct - 30.0))


def _part_whole_kpi_comparisons(
    kpis: list[KpiFact],
    widgets: list[dict[str, Any]],
    dashboard: dict[str, Any],
) -> list[ComparisonFact]:
    """Emit a rate only when KPIs match a profiled subset/total relationship."""
    by_id = {str(w.get("id") or ""): w for w in widgets if isinstance(w, dict)}
    col_to_kpi: dict[str, KpiFact] = {}
    extra_pairs: list[tuple[str, str]] = []
    for kpi in kpis:
        widget = by_id.get(kpi.widget_id) or {}
        quality = widget.get("quality") if isinstance(widget.get("quality"), dict) else {}
        col = str(quality.get("metric_column") or "").strip().lower()
        if col:
            col_to_kpi[col] = kpi
        part_of = str(quality.get("part_of") or "").strip().lower()
        if col and part_of:
            extra_pairs.append((col, part_of))

    pairs: list[tuple[str, str]] = []
    for item in dashboard.get("part_whole_pairs") or []:
        if isinstance(item, dict) and item.get("part") and item.get("whole"):
            pairs.append((str(item["part"]).lower(), str(item["whole"]).lower()))
    pairs.extend(extra_pairs)

    out: list[ComparisonFact] = []
    seen: set[tuple[str, str]] = set()
    for part, whole in pairs:
        key = (part, whole)
        if key in seen or part == whole:
            continue
        seen.add(key)
        part_kpi = col_to_kpi.get(part)
        whole_kpi = col_to_kpi.get(whole)
        if part_kpi is None or whole_kpi is None:
            continue
        if part_kpi.value is None or whole_kpi.value is None or whole_kpi.value == 0:
            continue
        if part_kpi.value < 0 or whole_kpi.value < 0:
            continue
        if part_kpi.value > whole_kpi.value:
            continue
        rate = part_kpi.value / whole_kpi.value
        if rate > 1:
            continue
        out.append(
            ComparisonFact(
                label=f"{part_kpi.title} / {whole_kpi.title}",
                left=part_kpi.title,
                right=whole_kpi.title,
                ratio=round(rate, 4),
                rate_pct=round(rate * 100.0, 1),
                significance_score=min(40.0, rate * 40.0),
            )
        )
    return out


def extract_dashboard_insight_facts(
    dashboard: dict[str, Any],
    *,
    semantic: dict[str, Any] | None = None,
) -> DashboardInsightFacts:
    """Build bounded facts from executed dashboard widgets only."""
    _ = semantic
    dash_id = str(dashboard.get("id") or "")
    filters = list(dashboard.get("filters") or [])
    widgets = [w for w in dashboard.get("widgets") or [] if isinstance(w, dict)]

    kpis: list[KpiFact] = []
    categories: list[CategoryFact] = []
    trends: list[TrendFact] = []
    comparisons: list[ComparisonFact] = []
    errors: list[WidgetErrorFact] = []
    source_ids: list[str] = []

    successful_data = 0
    has_breakdown_or_trend = False

    for widget in widgets:
        if not _is_source_widget(widget):
            continue
        wid = str(widget.get("id") or "")
        title = str(widget.get("title") or wid)
        wtype = str(widget.get("type") or "")

        if widget.get("error"):
            errors.append(WidgetErrorFact(widget_id=wid, title=title, error=str(widget["error"])))
            continue

        if wtype == "kpi":
            val = _as_number(widget.get("value"))
            if val is None:
                rows = widget.get("rows") or []
                if rows and isinstance(rows[0], dict):
                    val = _as_number(rows[0].get("value"))
            result_meta = widget.get("result") if isinstance(widget.get("result"), dict) else {}
            if val is None:
                val = _as_number(result_meta.get("raw_value"))
            # `null_result` distinguishes a query that legitimately returned
            # NULL (e.g. SUM() over zero matching rows) from a widget that
            # simply never got a value (not yet run, or an error) — only the
            # former is worth keeping as a fact.
            if val is None and not widget.get("null_result"):
                continue
            source_ids.append(wid)
            successful_data += 1
            kpis.append(
                KpiFact(
                    widget_id=wid,
                    title=title,
                    value=val,
                    formatted_value=_format_number(val) if val is not None else "No data",
                    value_kind=str(result_meta.get("value_kind") or widget.get("value_kind") or "amount"),
                    aggregation=str(result_meta.get("aggregation") or widget.get("aggregation") or "") or None,
                    significance_score=5.0,
                )
            )
            continue

        rows = widget.get("rows") or []
        result_meta = widget.get("result") if isinstance(widget.get("result"), dict) else {}
        pairs: list[tuple[str, float]] = []
        share_by_label: dict[str, float | None] = {}
        if result_meta.get("points"):
            for point in result_meta["points"]:
                if not isinstance(point, dict):
                    continue
                label = str(point.get("label") or "").strip()
                val = _as_number(point.get("raw_value"))
                if label and val is not None:
                    pairs.append((label, val))
                    share = point.get("share_of_series")
                    if isinstance(share, (int, float)):
                        share_by_label[label] = float(share) * 100.0
        if not pairs:
            pairs = _parse_category_rows(rows)
        if not pairs:
            continue
        if result_meta.get("labels_measure_like") or result_meta.get("meaningful") is False:
            continue
        useful = result_meta.get("useful_point_count")
        try:
            useful_n = int(useful) if useful is not None else None
        except (TypeError, ValueError):
            useful_n = None
        if useful_n is not None and useful_n < 2 and str(widget.get("chart") or "") in {"bar", "pie", ""}:
            continue

        source_ids.append(wid)
        successful_data += 1
        pairs_sorted = sorted(pairs, key=lambda x: x[1], reverse=True)
        value_kind = str(result_meta.get("value_kind") or widget.get("value_kind") or "amount")
        # Percentages, rates and averages are not additive: a "total" or a
        # "share of series total" for them is a meaningless number.
        additive = value_kind not in _NON_ADDITIVE_KINDS
        total = sum(v for _, v in pairs_sorted) if additive else 0.0
        if additive and result_meta.get("series_total") is not None:
            series_total = _as_number(result_meta.get("series_total"))
            if series_total:
                total = series_total
        if not additive:
            share_by_label = {}
        top_entries: list[CategoryEntry] = []
        for label, val in pairs_sorted[:3]:
            share = share_by_label.get(label)
            if share is None:
                share = (val / total * 100.0) if total else None
            top_entries.append(
                CategoryEntry(label=label, value=val, share_pct=round(share, 1) if share is not None else None)
            )
        bottom_entries: list[CategoryEntry] = []
        for label, val in pairs_sorted[-3:][::-1]:
            share = share_by_label.get(label)
            if share is None:
                share = (val / total * 100.0) if total else None
            bottom_entries.append(
                CategoryEntry(label=label, value=val, share_pct=round(share, 1) if share is not None else None)
            )
        top1_pct = top_entries[0].share_pct if top_entries else None
        chart = widget.get("chart")
        is_line = chart == "line" or (
            chart not in {"bar", "pie", "donut"}
            and wtype == "chart"
            and len(pairs_sorted) >= 3
        )

        if is_line and len(pairs_sorted) >= 3:
            has_breakdown_or_trend = True
            # Time order, never value order: `pairs_sorted` is ranked by value,
            # which turned every "first -> last" change into "max -> min".
            chronological = _chronological(pairs)
            first_val = chronological[0][1]
            last_val = chronological[-1][1]
            change_abs = last_val - first_val
            change_pct = (change_abs / first_val * 100.0) if first_val not in (0, None) else None
            anomalies = _zscore_anomalies(chronological)
            trends.append(
                TrendFact(
                    widget_id=wid,
                    title=title,
                    points=len(chronological),
                    first_value=first_val,
                    last_value=last_val,
                    change_abs=round(change_abs, 2),
                    change_pct=round(change_pct, 1) if change_pct is not None else None,
                    direction=_trend_direction(change_pct),
                    potential_anomalies=anomalies,
                    significance_score=_score_trend(change_pct, anomalies),
                )
            )
        else:
            has_breakdown_or_trend = True
            categories.append(
                CategoryFact(
                    widget_id=wid,
                    title=title,
                    chart=str(chart) if chart else None,
                    top=top_entries,
                    bottom=bottom_entries,
                    total=round(total, 2) if total else None,
                    concentration_top1_pct=top1_pct,
                    value_kind=value_kind,
                    aggregation=str(result_meta.get("aggregation") or widget.get("aggregation") or "") or None,
                    dimension=str(widget.get("dimension") or "") or None,
                    category_count=int(result_meta.get("category_count") or len(pairs_sorted)),
                    meaningful=True,
                    significance_score=_score_concentration(top1_pct),
                )
            )

    comparisons.extend(_part_whole_kpi_comparisons(kpis, widgets, dashboard))

    status = InsightFactStatus.OK
    insufficient_reason: str | None = None

    if successful_data == 0:
        status = InsightFactStatus.INSUFFICIENT_DATA
        if errors and len(errors) == len([w for w in widgets if _is_source_widget(w)]):
            insufficient_reason = "Widgets failed to load — fix errors and refresh."
        elif filters:
            insufficient_reason = "No rows match the current filters."
        else:
            insufficient_reason = (
                "Not enough dashboard data yet. Add KPIs or charts with results, then refresh."
            )
    elif len(kpis) == 1 and not has_breakdown_or_trend:
        status = InsightFactStatus.INSUFFICIENT_DATA
        insufficient_reason = "Add at least one breakdown or trend chart for richer insights."
    elif errors:
        status = InsightFactStatus.PARTIAL_DATA

    facts = DashboardInsightFacts(
        dashboard_id=dash_id,
        status=status,
        insufficient_reason=insufficient_reason,
        active_filters=filters,
        kpis=kpis,
        categories=categories,
        trends=trends,
        comparisons=comparisons,
        widget_errors=errors,
        source_widget_ids=source_ids,
    )
    facts.ranked_highlights = _build_ranked_highlights(facts)
    return facts


def _build_ranked_highlights(facts: DashboardInsightFacts) -> list[str]:
    bullets: list[tuple[float, str]] = []

    for trend in sorted(facts.trends, key=lambda t: t.significance_score, reverse=True):
        if trend.change_pct is not None and trend.direction != "flat":
            bullets.append(
                (
                    trend.significance_score + 10.0,
                    f"{trend.title}: {trend.direction} {abs(trend.change_pct):.1f}% "
                    f"({_format_number(trend.first_value or 0)} → "
                    f"{_format_number(trend.last_value or 0)}).",
                )
            )
        for anomaly in trend.potential_anomalies:
            bullets.append(
                (
                    trend.significance_score + abs(anomaly.z_score) * 5.0,
                    f"Potential anomaly in {trend.title} at {anomaly.period}: "
                    f"{_format_number(anomaly.value)} (z={anomaly.z_score:.1f}).",
                )
            )

    for cat in sorted(facts.categories, key=lambda c: c.significance_score, reverse=True):
        if not cat.meaningful or not cat.top:
            continue
        top = cat.top[0]
        kind = cat.value_kind or "amount"
        if kind == "rank":
            # A position (1 = best), not a measure: "highest is North India
            # at 4 (40% of the total)" is nonsense. The measure it ranks has
            # its own bullet.
            continue
        value = _format_number(top.value)
        # 100.0 * AVG(flag) is classified "average" by its SQL shape, but a
        # title saying percent/rate % is the better guide to how it reads.
        if kind == "percentage" or re.search(r"percent|\bpct\b|%", cat.title.lower()):
            value = f"{top.value:,.2f}%"
        if cat.category_count <= 1:
            # A single entity (a "which X has the most" answer): a share of
            # a one-row total is always 100% and says nothing.
            text = f"{cat.title}: {top.label} — {value}."
        else:
            tied = [t.label for t in cat.top if t.value == top.value]
            share = ""
            if kind not in _NON_ADDITIVE_KINDS and top.share_pct is not None:
                share = f" ({top.share_pct:.1f}% of the total)"
            if len(tied) > 1:
                names = ", ".join(tied[:4]) + (f" and {len(tied) - 4} more" if len(tied) > 4 else "")
                text = f"{cat.title}: tie between {names} at {value} each."
            else:
                text = f"{cat.title}: highest is {top.label} at {value}{share}."
        bullets.append((cat.significance_score + 8.0, text))

    for comp in sorted(facts.comparisons, key=lambda c: c.significance_score, reverse=True):
        if comp.rate_pct is not None:
            bullets.append(
                (
                    comp.significance_score + 5.0,
                    f"{comp.label}: {comp.rate_pct:.1f}%.",
                )
            )
        elif comp.ratio is not None:
            bullets.append((comp.significance_score + 5.0, f"{comp.label}: ratio {comp.ratio:.2f}x."))

    for kpi in facts.kpis:
        bullets.append((kpi.significance_score, f"{kpi.title}: {kpi.formatted_value}."))

    if facts.status == InsightFactStatus.PARTIAL_DATA and facts.widget_errors:
        names = ", ".join(e.title for e in facts.widget_errors[:3])
        bullets.append((100.0, f"Some widgets were unavailable: {names}."))

    bullets.sort(key=lambda x: x[0], reverse=True)
    return [text for _, text in bullets[:6]]


def facts_fingerprint(facts: DashboardInsightFacts) -> str:
    payload = {
        "status": facts.status.value,
        "filters": facts.active_filters,
        "kpis": [(k.widget_id, k.value) for k in facts.kpis],
        "categories": [
            (c.widget_id, c.total, c.concentration_top1_pct, [(t.label, t.value) for t in c.top])
            for c in facts.categories
        ],
        "trends": [
            (
                t.widget_id,
                t.last_value,
                t.change_pct,
                [(a.period, a.value, a.z_score) for a in t.potential_anomalies],
            )
            for t in facts.trends
        ],
        "errors": [(e.widget_id, e.error) for e in facts.widget_errors],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def render_deterministic_insight_markdown(facts: DashboardInsightFacts) -> str:
    # No leading "## AI Insights" heading here — the widget that displays
    # this content already renders its own title ("AI Insights"), so a
    # heading baked into the markdown just duplicates it on screen.
    if facts.status == InsightFactStatus.INSUFFICIENT_DATA:
        return facts.insufficient_reason or "Not enough dashboard data to generate insights."

    lines: list[str] = []
    for bullet in facts.ranked_highlights:
        lines.append(f"- {bullet}")
    if not facts.ranked_highlights:
        lines.append("- No notable patterns detected in the current dashboard data.")
    return "\n".join(lines)


def collect_allowed_numeric_values(facts: DashboardInsightFacts) -> set[float]:
    allowed: set[float] = set()

    def add(v: float | None) -> None:
        if v is None or not math.isfinite(v):
            return
        allowed.add(v)
        allowed.add(round(v, 1))
        allowed.add(round(v, 2))
        allowed.add(abs(v))

    for k in facts.kpis:
        add(k.value)
    for c in facts.categories:
        add(c.total)
        add(c.concentration_top1_pct)
        for entry in c.top + c.bottom:
            add(entry.value)
            add(entry.share_pct)
    for t in facts.trends:
        add(t.first_value)
        add(t.last_value)
        add(t.change_abs)
        add(t.change_pct)
        add(abs(t.change_pct) if t.change_pct is not None else None)
        for a in t.potential_anomalies:
            add(a.value)
            add(a.z_score)
            add(abs(a.z_score))
    for cmp in facts.comparisons:
        add(cmp.ratio)
        add(cmp.rate_pct)
    return allowed


_NUMBER_RE = re.compile(
    r"(?<![\w])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![\w])"
)


def _parse_numeric_token(token: str) -> float | None:
    return _as_number(token.strip().rstrip("%").replace(",", ""))


def _numeric_matches_allowed(value: float, allowed: set[float]) -> bool:
    if value in allowed:
        return True
    for ref in allowed:
        if ref == 0:
            if abs(value) < 0.01:
                return True
        elif abs(value - ref) <= max(0.05, abs(ref) * 0.02):
            return True
        elif abs(value - ref * 100) <= max(0.05, abs(ref * 100) * 0.02):
            return True
        elif abs(value / 100 - ref) <= max(0.0005, abs(ref) * 0.02):
            return True
    return False


def validate_narrative_numbers(content: str, facts: DashboardInsightFacts) -> bool:
    from app.agent.insight_validation import validate_narrative_numbers as _validate

    return _validate(content, facts)
