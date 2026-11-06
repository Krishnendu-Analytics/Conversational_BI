"""Generic analytical usefulness gate and structured decision logging.

Rules use profile statistics and executed result series only — never
industry- or column-name-specific logic.
"""

from __future__ import annotations

import json
import logging
import math
import re
from statistics import pstdev
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.dataset_profiler import (
    DatasetProfile,
    ProfileField,
    ProfileMetric,
    values_are_measure_like_labels,
)

logger = logging.getLogger(__name__)

PlannerSource = Literal["openai", "heuristic"]
ValidationResult = Literal["proposed", "accepted", "rejected", "demoted"]
DecisionStage = Literal["plan", "result"]

HIGH_UNIQUENESS = 0.8
HIGH_CARDINALITY = 40
MIN_TREND_POINTS = 3
FLAT_RELATIVE_RANGE = 0.02
MIN_USEFUL_SHARE = 0.05
MIN_USEFUL_CATEGORY_POINTS = 2
_DISTINCT_RE = re.compile(r"COUNT\s*\(\s*DISTINCT\s+(.+?)\s*\)", re.IGNORECASE)

ValueKind = Literal["count", "amount", "rate", "ratio", "percentage"]


class AnalysisDecision(BaseModel):
    analysis_id: str
    purpose: str
    metric: str
    dimension: str | None = None
    aggregation: str = ""
    chart_type: str = ""
    cardinality: int | None = None
    uniqueness_ratio: float | None = None
    result_point_count: int | None = None
    variance: float | None = None
    spread: float | None = None
    planner_source: PlannerSource
    stage: DecisionStage
    validation_result: ValidationResult
    useful: bool
    reason: str
    unavailable_stats: list[str] = Field(default_factory=list)

    def log(self) -> None:
        logger.info("analysis_decision %s", json.dumps(self.model_dump(mode="json"), default=str))


def planner_source_from_build_mode(build_mode: str | None) -> PlannerSource:
    mode = (build_mode or "").lower()
    if "llm" in mode or mode in {"openai", "analytical_llm", "semantic_llm"}:
        return "openai"
    return "heuristic"


def _norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _ident(text: str | None) -> str:
    raw = re.sub(r'["`\[\]]', "", text or "").strip()
    return raw.split(".")[-1].strip().lower()


def distinct_target_column(expression: str | None) -> str | None:
    match = _DISTINCT_RE.search(expression or "")
    if not match:
        return None
    return _ident(match.group(1)) or None


def widget_numeric_series(widget: dict[str, Any]) -> list[float]:
    values: list[float] = []
    rows = widget.get("rows") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if "value" in row:
            number = _as_number(row.get("value"))
        else:
            number = next(
                (_as_number(v) for v in row.values() if _as_number(v) is not None),
                None,
            )
        if number is not None:
            values.append(number)
    if not values and widget.get("type") == "kpi":
        number = _as_number(widget.get("value"))
        if number is not None:
            values.append(number)
    return values


def infer_value_kind(*, metric_kind: str | None, aggregation: str | None) -> ValueKind:
    kind = (metric_kind or "").strip().lower()
    agg = (aggregation or "").strip().lower()
    if kind in {"count", "row_count", "distinct"} or agg in {"count", "count_distinct"}:
        return "count"
    if kind == "rate":
        return "rate"
    if kind == "derived":
        return "ratio"
    if kind == "percentage":
        return "percentage"
    return "amount"


def useful_category_count(values: list[float]) -> int:
    total = sum(v for v in values if v > 0)
    if total <= 0:
        return 0
    return sum(1 for v in values if v > 0 and (v / total) >= MIN_USEFUL_SHARE)


def widget_category_pairs(widget: dict[str, Any]) -> list[tuple[str, float]]:
    """Label/value pairs from executed rows. Prefer the SQL `value` alias."""
    rows = widget.get("rows") or []
    if not rows or not isinstance(rows[0], dict):
        if widget.get("type") == "kpi":
            number = _as_number(widget.get("value"))
            if number is not None:
                return [("", number)]
        return []
    first = rows[0]
    keys = list(first.keys())
    value_key = next((k for k in keys if str(k).lower() == "value"), None)
    if value_key is None:
        value_key = next((k for k in keys if _as_number(first.get(k)) is not None), None)
    if value_key is None:
        return []
    label_key = next(
        (k for k in keys if k != value_key and str(k).lower() != "value"),
        None,
    )
    pairs: list[tuple[str, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = _as_number(row.get(value_key))
        if number is None:
            continue
        label = str(row.get(label_key) if label_key else "").strip()
        pairs.append((label, number))
    return pairs


def attach_canonical_result(widget: dict[str, Any]) -> dict[str, Any]:
    """Preserve raw metric values and a single share definition for charts/insights."""
    meta = _quality_meta(widget)
    kind = infer_value_kind(
        metric_kind=str(meta.get("metric_kind") or widget.get("value_kind") or ""),
        aggregation=str(widget.get("aggregation") or meta.get("aggregation") or ""),
    )
    # A derived KPI declared as a 0–100 percentage (e.g. "Rejected %") keeps
    # that kind; the generic "derived" metric kind would downgrade it to ratio.
    if widget.get("value_kind") == "percentage":
        kind = "percentage"
    widget["value_kind"] = kind
    pairs = widget_category_pairs(widget)
    values = [v for _, v in pairs]
    if str(widget.get("type") or "") == "kpi":
        raw = _as_number(widget.get("value"))
        if raw is None and pairs:
            raw = pairs[0][1]
        widget["result"] = {
            "value_kind": kind,
            "aggregation": widget.get("aggregation") or meta.get("aggregation"),
            "raw_value": raw,
            "denominator": None,
            "is_percentage": kind == "percentage",
            "points": [],
            "series_total": raw,
            "category_count": 0,
            "useful_point_count": 1 if raw is not None else 0,
            "labels_measure_like": False,
        }
        return widget

    total = sum(v for v in values if v is not None)
    points = []
    labels: list[str] = []
    for label, val in pairs:
        labels.append(label)
        share = (val / total) if total else None
        points.append(
            {
                "label": label,
                "raw_value": val,
                "share_of_series": round(share, 6) if share is not None else None,
            }
        )
    widget["result"] = {
        "value_kind": kind,
        "aggregation": widget.get("aggregation") or meta.get("aggregation"),
        "raw_value": None,
        "denominator": total if kind in {"count", "amount"} and total else None,
        "is_percentage": kind == "percentage",
        "points": points,
        "series_total": total if pairs else None,
        "category_count": len(pairs),
        "useful_point_count": useful_category_count(values),
        "labels_measure_like": values_are_measure_like_labels(labels),
    }
    logger.info(
        "canonical_result widget=%s kind=%s agg=%s points=%s useful=%s measure_like=%s",
        widget.get("id"),
        kind,
        widget.get("aggregation"),
        len(points),
        widget["result"]["useful_point_count"],
        widget["result"]["labels_measure_like"],
    )
    return widget


def _series_stats(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    spread = max(values) - min(values)
    try:
        variance = float(pstdev(values) ** 2) if len(values) > 1 else 0.0
    except Exception:  # noqa: BLE001
        variance = None
    return variance, spread


def _is_flat(values: list[float]) -> bool:
    if len(values) < 2:
        return False
    hi, lo = max(values), min(values)
    scale = max(abs(hi), abs(lo), 1.0)
    return (hi - lo) <= FLAT_RELATIVE_RANGE * scale


def _unavailable(
    *,
    cardinality: int | None,
    uniqueness: float | None,
    point_count: int | None,
    variance: float | None,
    spread: float | None,
) -> list[str]:
    missing: list[str] = []
    if cardinality is None:
        missing.append("cardinality")
    if uniqueness is None:
        missing.append("uniqueness_ratio")
    if point_count is None:
        missing.append("result_point_count")
    if variance is None:
        missing.append("variance")
    if spread is None:
        missing.append("spread")
    return missing


def _has_real_measure(profile: DatasetProfile) -> bool:
    return any(
        metric.kind in {"additive", "count"} and metric.kind != "row_count"
        for metric in [*profile.counts, *profile.measures]
    )


def _is_row_count_metric(metric: ProfileMetric | None, analysis: Any) -> bool:
    if metric is not None and metric.kind == "row_count":
        return True
    expr = (getattr(analysis, "metric_expression", None) or "").replace(" ", "").upper()
    if expr == "COUNT(*)":
        return True
    name = _norm(getattr(analysis, "metric", None) or "")
    return name in {"recordcount", "rowcount", "totalrecords"}


def _is_top_n(analysis: Any) -> bool:
    purpose = (getattr(analysis, "purpose", None) or "").lower()
    viz = (getattr(analysis, "visualization", None) or "").lower()
    title = (getattr(analysis, "title", None) or "").lower()
    if viz == "table":
        return True
    return any(
        token in purpose or token in title
        for token in ("top", "rank", "leaderboard")
    )


def _dimension_field(profile: DatasetProfile, analysis: Any) -> ProfileField | None:
    name = getattr(analysis, "dimension", None)
    if not name:
        return None
    found = profile.dimension_by_name(name)
    if found is not None:
        return found
    key = (name or "").strip().lower()
    for item in profile.identifiers:
        if item.name.lower() == key or item.column.lower() == key:
            return item
    return None


def _metric_obj(profile: DatasetProfile, analysis: Any) -> ProfileMetric | None:
    return profile.metric_by_name(getattr(analysis, "metric", None) or "")


def _same_field(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _norm(left) == _norm(right) or _ident(left) == _ident(right)


def _is_distinct_grouped_by_self(
    metric: ProfileMetric | None,
    dim: ProfileField | None,
    analysis: Any,
) -> bool:
    if dim is None:
        return False
    target = distinct_target_column(getattr(analysis, "metric_expression", None) or "")
    if metric is not None and not target:
        target = distinct_target_column(metric.expression)
    if metric is not None and metric.kind == "distinct" and metric.column:
        target = target or metric.column
    if not target:
        return False
    return _same_field(target, dim.column) or _same_field(target, dim.name)


def _is_tautological_combo(
    metric: ProfileMetric | None,
    dim: ProfileField | None,
    analysis: Any,
) -> bool:
    if dim is None:
        return False
    if _is_distinct_grouped_by_self(metric, dim, analysis):
        return True
    if metric is not None and metric.column and (
        _same_field(metric.column, dim.column) or _same_field(metric.column, dim.name)
    ):
        # Grouping a field by itself (measure column used as its own dimension).
        return True
    return False


def _chart_type(analysis: Any) -> str:
    return str(getattr(analysis, "visualization", None) or getattr(analysis, "chart", None) or "")


def _decision(
    analysis: Any,
    *,
    source: PlannerSource,
    stage: DecisionStage,
    result: ValidationResult,
    reason: str,
    cardinality: int | None = None,
    uniqueness: float | None = None,
    point_count: int | None = None,
    variance: float | None = None,
    spread: float | None = None,
) -> AnalysisDecision:
    useful = result in {"accepted", "demoted"}
    decision = AnalysisDecision(
        analysis_id=str(getattr(analysis, "id", None) or ""),
        purpose=str(getattr(analysis, "purpose", None) or ""),
        metric=str(getattr(analysis, "metric", None) or ""),
        dimension=getattr(analysis, "dimension", None),
        aggregation=str(getattr(analysis, "aggregation", None) or ""),
        chart_type=_chart_type(analysis),
        cardinality=cardinality,
        uniqueness_ratio=uniqueness,
        result_point_count=point_count,
        variance=variance,
        spread=spread,
        planner_source=source,
        stage=stage,
        validation_result=result,
        useful=useful,
        reason=reason,
        unavailable_stats=_unavailable(
            cardinality=cardinality,
            uniqueness=uniqueness,
            point_count=point_count,
            variance=variance,
            spread=spread,
        ),
    )
    decision.log()
    return decision


def evaluate_plan_analyses(
    analyses: list[Any],
    profile: DatasetProfile,
    *,
    source: PlannerSource,
) -> tuple[list[Any], list[AnalysisDecision]]:
    """Plan-time usefulness filter. May demote high-cardinality bar/pie to table."""
    decisions: list[AnalysisDecision] = []
    for analysis in analyses:
        dim = _dimension_field(profile, analysis)
        decisions.append(
            _decision(
                analysis,
                source=source,
                stage="plan",
                result="proposed",
                reason="Proposed by planner; not yet validated.",
                cardinality=dim.cardinality if dim is not None else None,
                uniqueness=dim.uniqueness_ratio if dim is not None else None,
            )
        )

    kept: list[Any] = []
    seen_keys: set[tuple[str, str, str]] = set()
    has_measure = _has_real_measure(profile)
    kept_measure_dims: set[str] = set()

    for analysis in analyses:
        metric = _metric_obj(profile, analysis)
        dim = _dimension_field(profile, analysis)
        card = dim.cardinality if dim is not None else None
        uniq = dim.uniqueness_ratio if dim is not None else None
        viz = _chart_type(analysis)
        key = (
            str(getattr(analysis, "metric", "") or "").lower(),
            str(getattr(analysis, "dimension", "") or "").lower(),
            str(getattr(analysis, "grain", "") or ""),
        )

        if key in seen_keys:
            decisions.append(
                _decision(
                    analysis,
                    source=source,
                    stage="plan",
                    result="rejected",
                    reason="Duplicate metric/dimension analysis.",
                    cardinality=card,
                    uniqueness=uniq,
                )
            )
            continue

        if dim is not None and viz in {"bar", "pie", "table", "line"}:
            if values_are_measure_like_labels(list(dim.sample_values or [])):
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason=(
                            "Dimension sample values are percentage/measure-like labels, "
                            "not categorical groupings."
                        ),
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            if _is_distinct_grouped_by_self(metric, dim, analysis):
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason="COUNT(DISTINCT D) grouped by D is tautological.",
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            if _is_tautological_combo(metric, dim, analysis):
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason="Metric and dimension refer to the same field.",
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            if uniq is not None and uniq >= HIGH_UNIQUENESS and (card is None or card >= 15):
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason=(
                            "Dimension uniqueness is too high for a useful grouping "
                            f"({uniq:.2f} >= {HIGH_UNIQUENESS})."
                        ),
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            if _is_row_count_metric(metric, analysis) and has_measure and viz != "kpi":
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason="COUNT(*) breakdown skipped because a real additive/count measure exists.",
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            dim_key = str(getattr(analysis, "dimension", "") or "").lower()
            if (
                _is_row_count_metric(metric, analysis)
                and dim_key in kept_measure_dims
                and viz != "kpi"
            ):
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason="Row-count breakdown is less informative than a measure breakdown of the same dimension.",
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            if card == 1 and viz in {"bar", "pie"}:
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason="Bar/pie with a single category is not informative.",
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                continue
            if (
                card is not None
                and card > HIGH_CARDINALITY
                and viz in {"bar", "pie"}
                and not _is_top_n(analysis)
            ):
                analysis.visualization = "table"
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="demoted",
                        reason=(
                            f"High cardinality ({card}) demoted from {viz} to table "
                            "(no explicit top-N request)."
                        ),
                        cardinality=card,
                        uniqueness=uniq,
                    )
                )
                seen_keys.add(key)
                kept.append(analysis)
                continue

        if viz == "line":
            date = None
            time_field = getattr(analysis, "time_field", None)
            if time_field:
                date = profile.dimension_by_name(str(time_field))
            if date is None and profile.dates:
                date = profile.dates[0]
            buckets = date.date_bucket_count if date is not None else None
            if buckets is not None and buckets < MIN_TREND_POINTS:
                decisions.append(
                    _decision(
                        analysis,
                        source=source,
                        stage="plan",
                        result="rejected",
                        reason=(
                            f"Time series has {buckets} date bucket(s); "
                            f"need at least {MIN_TREND_POINTS} for a trend."
                        ),
                        cardinality=card,
                        uniqueness=uniq,
                        point_count=buckets,
                    )
                )
                continue

        seen_keys.add(key)
        if dim is not None and not _is_row_count_metric(metric, analysis):
            kept_measure_dims.add(str(getattr(analysis, "dimension", "") or "").lower())
        kept.append(analysis)
        decisions.append(
            _decision(
                analysis,
                source=source,
                stage="plan",
                result="accepted",
                reason="Passed plan-time usefulness checks.",
                cardinality=card,
                uniqueness=uniq,
            )
        )
    return kept, decisions


def _widget_chart_kind(widget: dict[str, Any]) -> str:
    if str(widget.get("type") or "") == "pie" or widget.get("chart") == "pie":
        return "pie"
    if widget.get("chart") == "line":
        return "line"
    if str(widget.get("type") or "") == "table":
        return "table"
    if str(widget.get("type") or "") == "kpi":
        return "kpi"
    if str(widget.get("type") or "") in {"chart", "bar"}:
        return str(widget.get("chart") or "bar")
    return str(widget.get("type") or "")


def _quality_meta(widget: dict[str, Any]) -> dict[str, Any]:
    meta = widget.get("quality")
    return meta if isinstance(meta, dict) else {}


def _result_uniqueness(
    widget: dict[str, Any],
    values: list[float],
    profile: DatasetProfile | None,
) -> float | None:
    meta = _quality_meta(widget)
    if meta.get("uniqueness_ratio") is not None:
        try:
            return float(meta["uniqueness_ratio"])
        except (TypeError, ValueError):
            pass
    dim_name = widget.get("dimension")
    if profile is not None and dim_name:
        dim = profile.dimension_by_name(str(dim_name))
        if dim is not None and dim.uniqueness_ratio is not None:
            return dim.uniqueness_ratio
    kind = str(meta.get("metric_kind") or "")
    if kind in {"row_count", "count", "distinct"} and values:
        total = sum(v for v in values if v > 0)
        if total > 0:
            return round(min(1.0, len(values) / total), 6)
    row_count = profile.row_count if profile is not None else None
    if row_count and values:
        return round(min(1.0, len(values) / row_count), 6)
    return None


def evaluate_executed_widgets(
    dashboard: dict[str, Any],
    profile: DatasetProfile | None = None,
    *,
    source: PlannerSource | None = None,
) -> dict[str, Any]:
    """Drop executed widgets that are uninformative; log each decision."""
    planner = source or planner_source_from_build_mode(str(dashboard.get("build_mode") or ""))
    decisions: list[AnalysisDecision] = [
        AnalysisDecision.model_validate(item)
        for item in (dashboard.get("analysis_decisions") or [])
        if isinstance(item, dict)
    ]
    kept: list[dict[str, Any]] = []

    widgets = [w for w in (dashboard.get("widgets") or []) if isinstance(w, dict)]
    for widget in widgets:
        wtype = str(widget.get("type") or "")
        # Figures extracted from an attached document are the document's own
        # content, not a query result to second-guess: keep them as written.
        if wtype in {"ai_insight", "text"} or widget.get("source") == "document":
            kept.append(widget)
            continue

        class _Proxy:
            id = str(widget.get("id") or "")
            purpose = str(widget.get("purpose") or "")
            metric = str(widget.get("metric") or "")
            dimension = widget.get("dimension")
            aggregation = str(widget.get("aggregation") or "")
            visualization = _widget_chart_kind(widget)
            title = str(widget.get("title") or "")
            metric_expression = str(widget.get("metric_expression") or "")

        proxy = _Proxy()
        attach_canonical_result(widget)
        canonical = widget.get("result") if isinstance(widget.get("result"), dict) else {}
        values = widget_numeric_series(widget)
        point_count = len(values) if values else (len(widget.get("rows") or []) or None)
        variance, spread = _series_stats(values)
        meta = _quality_meta(widget)
        card = meta.get("cardinality")
        if card is not None:
            try:
                card = int(card)
            except (TypeError, ValueError):
                card = None
        uniqueness = _result_uniqueness(widget, values, profile)
        chart = proxy.visualization
        value_kind = str(canonical.get("value_kind") or widget.get("value_kind") or "")

        if chart == "pie" and value_kind in {"count", "amount"}:
            widget["chart"] = "bar"
            if str(widget.get("type") or "") == "pie":
                widget["type"] = "chart"
            chart = "bar"
            proxy.visualization = "bar"
            decisions.append(
                _decision(
                    proxy,
                    source=planner,
                    stage="result",
                    result="demoted",
                    reason="Count/amount series demoted from pie so raw values are not shown as percentages.",
                    cardinality=card,
                    uniqueness=uniqueness,
                    point_count=point_count,
                    variance=variance,
                    spread=spread,
                )
            )

        if not values and wtype != "kpi":
            rows = widget.get("rows") or []
            if not rows:
                decisions.append(
                    _decision(
                        proxy,
                        source=planner,
                        stage="result",
                        result="rejected",
                        reason="Empty result.",
                        cardinality=card,
                        uniqueness=uniqueness,
                        point_count=0,
                        variance=variance,
                        spread=spread,
                    )
                )
                continue
            if all(
                not isinstance(row, dict) or all(v is None or v == "" for v in row.values())
                for row in rows
            ):
                decisions.append(
                    _decision(
                        proxy,
                        source=planner,
                        stage="result",
                        result="rejected",
                        reason="All-null result.",
                        cardinality=card,
                        uniqueness=uniqueness,
                        point_count=point_count,
                        variance=variance,
                        spread=spread,
                    )
                )
                continue

        if wtype == "kpi":
            kept.append(widget)
            decisions.append(
                _decision(
                    proxy,
                    source=planner,
                    stage="result",
                    result="accepted",
                    reason="KPI result is a single aggregate value.",
                    cardinality=card,
                    uniqueness=uniqueness,
                    point_count=point_count,
                    variance=variance,
                    spread=spread,
                )
            )
            continue

        if chart == "line" and (point_count or 0) < MIN_TREND_POINTS:
            decisions.append(
                _decision(
                    proxy,
                    source=planner,
                    stage="result",
                    result="rejected",
                    reason=(
                        f"Trend has {point_count} time bucket(s); "
                        f"need at least {MIN_TREND_POINTS}."
                    ),
                    cardinality=card,
                    uniqueness=uniqueness,
                    point_count=point_count,
                    variance=variance,
                    spread=spread,
                )
            )
            continue

        if (
    chart in {"bar", "pie"}
    and (point_count or 0) <= 1
    and not dashboard.get("filters")
):
            decisions.append(
                _decision(
                    proxy,
                    source=planner,
                    stage="result",
                    result="rejected",
                    reason="Bar/pie has only 1 category.",
                    cardinality=card,
                    uniqueness=uniqueness,
                    point_count=point_count,
                    variance=variance,
                    spread=spread,
                )
            )
            continue

        if uniqueness is not None and uniqueness >= HIGH_UNIQUENESS and chart in {"bar", "pie", "table"}:
            card_n = card if isinstance(card, int) else (point_count or 0)
            if card_n >= 15 or card is None:
                decisions.append(
                    _decision(
                        proxy,
                        source=planner,
                        stage="result",
                        result="rejected",
                        reason=(
                            "Dimension uniqueness is too high for a useful grouping "
                            f"({uniqueness:.2f} >= {HIGH_UNIQUENESS})."
                        ),
                        cardinality=card,
                        uniqueness=uniqueness,
                        point_count=point_count,
                        variance=variance,
                        spread=spread,
                    )
                )
                continue

        if chart in {"bar", "pie", "table"} and canonical.get("labels_measure_like"):
            decisions.append(
                _decision(
                    proxy,
                    source=planner,
                    stage="result",
                    result="rejected",
                    reason=(
                        "Dimension labels are percentage/measure-like values, "
                        "not categorical groupings."
                    ),
                    cardinality=card,
                    uniqueness=uniqueness,
                    point_count=point_count,
                    variance=variance,
                    spread=spread,
                )
            )
            continue

        if chart in {"bar", "pie"} and not dashboard.get("filters"):
            useful = canonical.get("useful_point_count")
            try:
                useful_n = int(useful) if useful is not None else None
            except (TypeError, ValueError):
                useful_n = None
            if useful_n is not None and useful_n < MIN_USEFUL_CATEGORY_POINTS:
                decisions.append(
                    _decision(
                        proxy,
                        source=planner,
                        stage="result",
                        result="rejected",
                        reason=(
                            f"Only {useful_n} categor(y/ies) carry a meaningful share "
                            f"(>={MIN_USEFUL_SHARE:.0%} of series); breakdown is not informative."
                        ),
                        cardinality=card,
                        uniqueness=uniqueness,
                        point_count=point_count,
                        variance=variance,
                        spread=spread,
                    )
                )
                continue

        if (
    chart in {"bar", "pie"}
    and _is_flat(values)
    and not dashboard.get("filters")
):
            decisions.append(
                _decision(
                    proxy,
                    source=planner,
                    stage="result",
                    result="rejected",
                    reason="Series has near-zero variance (max approximately equals min).",
                    cardinality=card,
                    uniqueness=uniqueness,
                    point_count=point_count,
                    variance=variance,
                    spread=spread,
                )
            )
            continue

        kept.append(widget)
        decisions.append(
            _decision(
                proxy,
                source=planner,
                stage="result",
                result="accepted",
                reason="Passed result-level usefulness checks.",
                cardinality=card,
                uniqueness=uniqueness,
                point_count=point_count,
                variance=variance,
                spread=spread,
            )
        )

    out = dict(dashboard)
    out["widgets"] = kept
    out["analysis_decisions"] = [d.model_dump(mode="json") for d in decisions]
    return out


def merge_decisions(
    existing: list[Any],
    extra: list[AnalysisDecision],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in existing or []:
        if isinstance(item, AnalysisDecision):
            rows.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            rows.append(item)
    rows.extend(d.model_dump(mode="json") for d in extra)
    return rows
