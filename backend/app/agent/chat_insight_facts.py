"""Deterministic chat insight facts from query results."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import Field

from app.agent.dashboard_insight_facts import (
    DashboardInsightFacts,
    InsightFactStatus,
    extract_dashboard_insight_facts,
    facts_fingerprint,
)
from app.agent.insight_validation import GroundingContext, grounding_context_from_dashboard_facts
from app.agent.insight_validation import ComparisonDirectionFact, NumericClaim, _aliases_from_label
from app.agent.measure_kind import ADDITIVE_KINDS, is_code_like_name, normalize_name, resolve_column_kinds
from app.agent.query_plan import QueryPlan


class ChatInsightFacts(DashboardInsightFacts):
    """Facts derived from a single analytical query — domain-agnostic."""

    tenant_id: str = ""
    connection_id: str = ""
    question: str = ""
    sql: str = ""
    row_count: int = 0
    time_context: dict[str, Any] | None = None
    scope_key: str = ""
    period_comparisons: list[ComparisonDirectionFact] = Field(default_factory=list)
    # True for a plain listing/detail query (no aggregation requested) —
    # there is nothing to break down into KPIs/categories by design, so
    # narration should just acknowledge the rows rather than ask the LLM
    # to describe facts that are legitimately empty.
    is_listing: bool = False
    # Measure kind per result column (amount, count, percentage, ratio,
    # average, ...) — lets the UI chart percentages on their own axis.
    column_kinds: dict[str, str] = Field(default_factory=dict)
    # The executed rows themselves (trimmed). Derived facts can miss what a
    # reader sees at a glance in the result table — a name next to four
    # measures, a column the fact builder has no pattern for — so the
    # narrator sees the rows too, and every value in them is grounded.
    result_rows: list[dict[str, Any]] = Field(default_factory=list)


MAX_NARRATOR_ROWS = 20
MAX_NARRATOR_COLS = 10
MAX_NARRATOR_CELL = 80


def _trim_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed: list[dict[str, Any]] = []
    keys = list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
    # The narrator quotes what it sees: keep provenance columns (a literal
    # 'data_mart.fact_sales' AS source) out of its rows.
    shown = [k for k in keys if not _is_internal_text_column(rows, k)][:MAX_NARRATOR_COLS]
    for row in rows[:MAX_NARRATOR_ROWS]:
        out: dict[str, Any] = {}
        for key in shown:
            if key not in row:
                continue
            value = row[key]
            if isinstance(value, str) and len(value) > MAX_NARRATOR_CELL:
                value = value[: MAX_NARRATOR_CELL - 1] + "…"
            elif not isinstance(value, (str, int, float, bool, type(None))):
                value = str(value)
            out[key] = value
        trimmed.append(out)
    return trimmed


def _time_context_from_plan(plan: QueryPlan | None) -> dict[str, Any] | None:
    if plan is None or plan.time is None:
        return None
    return plan.time.model_dump(mode="json")


_DATE_PART_NAMES = {
    "year", "quarter", "month", "week", "day",
    "calendar_year", "calendar_quarter", "calendar_month", "calendar_week", "calendar_day",
    "fiscal_year", "fiscal_quarter", "fiscal_month",
    "year_number", "month_number", "quarter_number", "week_number", "day_number",
    "day_of_week", "day_of_month", "day_of_year",
}


def _is_identifier_like_key(key: str) -> bool:
    """True for columns that look like an ID/key, or a numeric date-part
    (year/month/quarter number) — neither is a real measure, and summing
    them (e.g. "month by quarter: Q4 has 12") is nonsense."""
    name = str(key).lower()
    return (
        name in {"id", "row_num", "rn", "key"}
        or name.endswith("_id")
        or name.endswith("_key")
        or name.endswith("_pk")
        or name in _DATE_PART_NAMES
        or is_code_like_name(name)
    )


_SQL_AGGREGATE_RE = re.compile(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_SQL_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def _sql_has_aggregation(sql: str) -> bool:
    """True if the SQL text aggregates at all (SUM/COUNT/AVG/... or GROUP
    BY). A plain listing query has neither — that's a more reliable signal
    than the query plan's own ``intent`` label, which isn't always set
    correctly for natural "show/list all X" phrasing."""
    text = sql or ""
    return bool(_SQL_AGGREGATE_RE.search(text) or _SQL_GROUP_BY_RE.search(text))


def _grain_label_keys(
    rows: list[dict[str, Any]], label_key: str, text_keys: list[str]
) -> list[str]:
    """Columns that together identify a row. When the primary label repeats
    (e.g. zone in a zone + state breakdown), later text columns are part of
    the grain — dropping them makes four "East" rows indistinguishable and
    hides the state level from the narrator entirely."""
    ordered = [label_key] + [k for k in text_keys if k != label_key]
    chosen: list[str] = []
    for key in ordered[:3]:
        chosen.append(key)
        labels = {_composite_label(r, chosen) for r in rows if isinstance(r, dict)}
        if len(labels) >= len(rows):
            break
    return chosen


def _humanize_key(key: str) -> str:
    """'late_dispatch_count' -> 'Late Dispatch Count' — result column aliases
    are shown to business users, who shouldn't see snake_case."""
    return str(key).replace("_", " ").strip().title() or str(key)


def _display_title(name: Any) -> str:
    """A metric/column name as a reader-facing title: plan metrics and SQL
    aliases are often snake_case ("total_planned_production_mt")."""
    text = str(name or "").strip()
    if text and " " not in text and "_" in text:
        return _humanize_key(text)
    return text


# Columns that describe where data came from, not what it is about.
_PROVENANCE_KEYS = frozenset({"source_table", "table_name", "source", "table", "schema_name", "source_schema"})
# "data_mart.fact_sales", "public.dim_customer": a qualified SQL identifier.
_QUALIFIED_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*){1,2}$")


def _is_internal_text_column(rows: list[dict[str, Any]], key: str) -> bool:
    """Never label a business answer with a table name ("East India /
    data_mart.fact_sales"): skip provenance columns and columns whose values
    are all qualified identifiers."""
    if str(key).lower() in _PROVENANCE_KEYS:
        return True
    values = [str(r.get(key)).strip() for r in rows[:50] if isinstance(r, dict) and r.get(key) not in (None, "")]
    return bool(values) and all(
        "_" in v and _QUALIFIED_IDENTIFIER_RE.match(v.lower()) for v in values
    )


def _composite_label(row: dict[str, Any], label_keys: list[str]) -> str:
    parts = [str(row.get(k)).strip() for k in label_keys if row.get(k) not in (None, "")]
    return " / ".join(parts)


_YEAR_KEYS = ("calendar_year", "fiscal_year", "year", "year_number")
_MONTH_NUM_KEYS = ("month_number", "calendar_month", "month", "fiscal_month")
_MONTH_NAME_KEYS = ("month_name",)
_QUARTER_KEYS = ("calendar_quarter", "fiscal_quarter", "quarter", "quarter_number")
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def _month_index(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 <= int(value) <= 12:
        return int(value)
    text = str(value or "").strip().lower()
    if text.isdigit() and 1 <= int(text) <= 12:
        return int(text)
    for i, name in enumerate(_MONTHS, start=1):
        if text and (text == name or text == name[:3]):
            return i
    return None


def _merge_period_columns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold calendar-part columns (year + month number/name, or year +
    quarter) into one chronological "period" label.

    Numeric year/month columns look like identifiers and were dropped, which
    left either no label at all (year + month_number -> "not enough data") or
    only a bare month name that repeats across years (two "October" rows,
    a chart axis reading "April, April, May, May"). Rows come back in
    calendar order so the result reads as the trend it is."""
    if not rows or not isinstance(rows[0], dict):
        return rows
    keys = {k.lower(): k for k in rows[0]}
    year = next((keys[k] for k in _YEAR_KEYS if k in keys), None)
    month_num = next((keys[k] for k in _MONTH_NUM_KEYS if k in keys), None)
    month_name = next((keys[k] for k in _MONTH_NAME_KEYS if k in keys), None)
    quarter = next((keys[k] for k in _QUARTER_KEYS if k in keys), None)
    month = month_name or month_num
    if "period" in keys or not (year and (month or quarter)):
        return rows
    parts = [k for k in (year, month_num, month_name, quarter) if k]

    def label(row: dict[str, Any]) -> tuple[str, tuple[int, int, int]]:
        y = row.get(year)
        try:
            y_int = int(float(y))
        except (TypeError, ValueError):
            y_int = 0
        m = (_month_index(row.get(month_name)) or _month_index(row.get(month_num))) if month else None
        if m:
            text = f"{_MONTHS[m - 1].capitalize()} {y_int}" if month_name else f"{y_int}-{m:02d}"
            return text, (y_int, m, 0)
        try:
            q = int(float(row.get(quarter))) if quarter else 0
        except (TypeError, ValueError):
            q = 0
        return (f"{y_int} Q{q}" if q else str(y_int)), (y_int, 0, q)

    merged: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for row in rows:
        text, sort_key = label(row)
        rest = {k: v for k, v in row.items() if k not in parts}
        merged.append((sort_key, {"period": text, **rest}))
    merged.sort(key=lambda item: item[0])
    return [row for _, row in merged]


def _rows_to_virtual_widgets(
    rows: list[dict[str, Any]],
    *,
    query_plan: QueryPlan | None,
    column_kinds: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    rows = _merge_period_columns(rows)
    kinds = column_kinds or {}

    if len(rows) == 1 and isinstance(rows[0], dict):
        row = rows[0]
        numeric_keys = [
            k
            for k, v in row.items()
            if isinstance(v, (int, float)) and not _is_identifier_like_key(k)
        ]
        text_keys = [
            k
            for k, v in row.items()
            if not isinstance(v, (int, float))
            and v not in (None, "")
            and not _is_identifier_like_key(k)
            and not _is_internal_text_column(rows, k)
        ]
        # A single row with one label and one measure is a "top-1" result
        # (e.g. "which customer has the highest sales?" -> GROUP BY ...
        # ORDER BY ... LIMIT 1). Narrate it as a named entity via the
        # category path (which produces an EntityLeaderFact), not as a
        # bare scalar KPI — the latter has no field for the label, so the
        # entity name would be silently dropped and only the number kept.
        # The same holds with several measures ("which seller has the most
        # delivery issues?" -> seller + issue count, qty, value, late %):
        # each measure is narrated against the entity, never as a bare KPI.
        if numeric_keys and text_keys:
            label = _composite_label(row, text_keys[:3])
            widgets: list[dict[str, Any]] = []
            for key in numeric_keys[:4]:
                metric = _display_title(
                    query_plan.metrics[0]
                    if len(numeric_keys) == 1 and query_plan and query_plan.metrics
                    else key
                )
                widgets.append(
                    {
                        "id": "chat-leader" if len(numeric_keys) == 1 else f"chat-leader-{key}",
                        "type": "chart",
                        "title": metric,
                        "chart": "bar",
                        "rows": [{"category": label, "value": row[key]}],
                        **({"value_kind": kinds[key]} if key in kinds else {}),
                    }
                )
            return widgets
        if len(numeric_keys) == 1:
            key = numeric_keys[0]
            title = _display_title(query_plan.metrics[0] if query_plan and query_plan.metrics else key)
            return [
                {
                    "id": "chat-scalar",
                    "type": "kpi",
                    "title": title,
                    "value": row[key],
                    "rows": [{"value": row[key]}],
                    **({"value_kind": kinds[key]} if key in kinds else {}),
                }
            ]
        if numeric_keys:
            widgets: list[dict[str, Any]] = []
            for key in numeric_keys[:4]:
                title = key.replace("_", " ").title()
                widgets.append(
                    {
                        "id": f"chat-kpi-{key}",
                        "type": "kpi",
                        "title": title,
                        "value": row[key],
                        "rows": [{"value": row[key]}],
                        **({"value_kind": kinds[key]} if key in kinds else {}),
                    }
                )
            return widgets

        # A single non-identifier column whose value is NULL (e.g. SUM()
        # over zero matching rows, such as "sales last quarter" with no
        # sales in that window) is a real, executed result — not a missing
        # one. Surface it as a null-valued KPI so the answer can say "no
        # data for this scope" instead of the misleading generic
        # "insufficient data", which is what happens if this row is left
        # to fall through: `None` fails every numeric check below, so it
        # gets miscategorized as a text column and silently dropped.
        non_identifier_keys = [k for k in row if not _is_identifier_like_key(k)]
        if len(non_identifier_keys) == 1 and row[non_identifier_keys[0]] is None:
            key = non_identifier_keys[0]
            title = _display_title(query_plan.metrics[0] if query_plan and query_plan.metrics else key)
            return [
                {
                    "id": "chat-scalar",
                    "type": "kpi",
                    "title": title,
                    "value": None,
                    "null_result": True,
                    "rows": [{"value": None}],
                }
            ]

    keys = list(rows[0].keys()) if isinstance(rows[0], dict) else []
    label_key = None
    value_key = None
    for candidate in ("category", "period", "label", "name", "region", "segment"):
        if candidate in keys:
            label_key = candidate
            break
    for candidate in ("value", "amount", "total", "count", "sum", "qty", "quantity"):
        if candidate in keys:
            value_key = candidate
            break

    text_keys = [
        k
        for k in keys
        if not isinstance(rows[0].get(k), (int, float))
        and not _is_identifier_like_key(k)
        and not _is_internal_text_column(rows, k)
    ]
    # Every non-identifier numeric column is a candidate measure — not just
    # the first one — so a result with several measures (e.g. revenue AND
    # profit by product) surfaces all of them instead of discarding all but
    # one column.
    value_keys = [value_key] if value_key else []
    if label_key is None or value_key is None:
        num_keys = [k for k in keys if isinstance(rows[0].get(k), (int, float))]
        measure_keys = [k for k in num_keys if not _is_identifier_like_key(k)]
        # If every numeric column is an identifier (e.g. a plain listing
        # query with no aggregate), there is no real measure to chart —
        # fall through to the generic table view rather than fabricating
        # one from a customer_key/order_id-style column.
        if text_keys and measure_keys:
            label_key = text_keys[0]
            value_keys = measure_keys[:4]
        else:
            label_key = None
            value_keys = []

    if label_key and value_keys:
        label_keys = _grain_label_keys(rows, label_key, text_keys)
        if len(label_keys) > 1:
            dim = ", ".join(_humanize_key(k) for k in label_keys)
        else:
            dim = _display_title(
                query_plan.dimensions[0]
                if query_plan and query_plan.dimensions
                else label_key
            )
        is_temporal = len(label_keys) == 1 and (
            label_key in {"period", "date", "month", "week", "year"}
            or bool(
                query_plan
                and query_plan.time
                and query_plan.time.field
                and str(query_plan.time.field).lower() in str(label_key).lower()
            )
        )
        chart = "line" if is_temporal and len(rows) >= 3 else "bar"
        widgets: list[dict[str, Any]] = []
        plan_metrics = {
            normalize_name(m): m for m in (query_plan.metrics if query_plan else []) if m
        }
        for vkey in value_keys:
            # Match plan metrics by name, never by position: the SQL may order
            # columns differently or add derived ones, and a positional match
            # would put one measure's title on another measure's numbers.
            metric = _display_title(plan_metrics.get(normalize_name(vkey)) or vkey)
            value_kind = kinds.get(vkey)
            parsed_rows = [
                {"category": _composite_label(row, label_keys), "value": row.get(vkey)}
                for row in rows
                if isinstance(row, dict)
            ]
            widget: dict[str, Any] = {
                "id": f"chat-breakdown-{vkey}",
                "type": "chart",
                "title": f"{metric} by {dim}",
                "chart": chart,
                "rows": parsed_rows,
            }
            if value_kind:
                widget["value_kind"] = value_kind
            widgets.append(widget)

        # A multi-level grouping (e.g. zone + state) also gets a subtotal
        # per top-level value for measures known to be additive (SUM/COUNT),
        # so "East" is described by its actual total rather than by whichever
        # single East row ranked highest. Rates, percentages, averages and
        # anything unclassified are never summed.
        if len(label_keys) > 1:
            top_key = label_keys[0]
            for vkey in value_keys:
                if kinds.get(vkey) not in ADDITIVE_KINDS:
                    continue
                totals: dict[str, float] = {}
                for row in rows:
                    val = row.get(vkey) if isinstance(row, dict) else None
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        continue
                    label = str(row.get(top_key) or "").strip()
                    if label:
                        totals[label] = totals.get(label, 0.0) + float(val)
                if len(totals) < 2 or len(totals) >= len(rows):
                    continue
                widgets.append(
                    {
                        "id": f"chat-rollup-{vkey}",
                        "type": "chart",
                        "title": f"{_humanize_key(vkey)} Total by {_humanize_key(top_key)}",
                        "chart": "bar",
                        "rows": [{"category": k, "value": v} for k, v in totals.items()],
                    }
                )
        return widgets

    return [
        {
            "id": "chat-table",
            "type": "chart",
            "title": "Query result",
            "chart": "bar",
            "rows": rows[:50],
        }
    ]


def extract_chat_insight_facts(
    rows: list[dict[str, Any]] | None,
    *,
    question: str = "",
    sql: str | None = None,
    query_plan: QueryPlan | dict[str, Any] | None = None,
    tenant_id: UUID | str | None = None,
    connection_id: str | None = None,
    filters: list[dict[str, Any]] | None = None,
    semantic_catalog: dict[str, Any] | None = None,
) -> ChatInsightFacts:
    """Build bounded facts from executed query rows only.

    Each result column's measure kind (additive amount vs. percentage,
    ratio, average...) comes from the semantic layer and the structure of
    the executed SQL, so aggregation stays correct for arbitrary joins and
    calculations regardless of how the columns are aliased.
    """
    plan: QueryPlan | None = None
    if query_plan is not None:
        if isinstance(query_plan, QueryPlan):
            plan = query_plan
        else:
            try:
                plan = QueryPlan.model_validate(query_plan)
            except Exception:  # noqa: BLE001
                plan = None

    row_list = [r for r in (rows or []) if isinstance(r, dict)]
    active_filters = list(filters or [])
    if plan and plan.filters and not active_filters:
        active_filters = [f.model_dump() for f in plan.filters]

    from app.agent.comparison_facts import extract_comparison_facts

    period_comparisons = extract_comparison_facts(row_list, query_plan=plan)

    column_kinds = resolve_column_kinds(
        list(row_list[0].keys()) if row_list else [],
        sql=sql,
        semantic_catalog=semantic_catalog,
    )
    pseudo_dashboard = {
        "id": "",
        "filters": active_filters,
        "widgets": _rows_to_virtual_widgets(
            row_list,
            query_plan=plan,
            column_kinds=column_kinds,
        ),
    }
    base = extract_dashboard_insight_facts(pseudo_dashboard)

    # Only treat this as a plain listing when nothing else was already
    # extracted — a real KPI/category/trend (e.g. a single-row scalar
    # lookup) must still be narrated normally, even from unaggregated SQL.
    is_listing_query = bool(
        row_list
        and not base.kpis
        and not base.categories
        and not base.trends
        and (
            (plan is not None and plan.intent == "detail")
            or not _sql_has_aggregation(sql or "")
        )
    )

    insufficient = base.insufficient_reason
    if not row_list:
        base.status = InsightFactStatus.INSUFFICIENT_DATA
        insufficient = "The query returned no rows."
    elif is_listing_query:
        # A plain listing query (no aggregation requested) has nothing to
        # break down into KPIs/categories — that's expected, not
        # "insufficient data". The result table already shows the rows;
        # the narrator just needs to acknowledge them honestly.
        base.status = InsightFactStatus.OK
        insufficient = None
    elif len(row_list) == 1 and base.status == InsightFactStatus.INSUFFICIENT_DATA:
        base.status = InsightFactStatus.OK
        insufficient = None
    elif len(row_list) >= 2 and not base.categories and not base.trends and base.kpis:
        base.status = InsightFactStatus.OK
        insufficient = None
    elif base.status == InsightFactStatus.INSUFFICIENT_DATA:
        # The dashboard-oriented reason ("Add KPIs or charts, then
        # refresh") makes no sense as a chat answer to a query that did
        # return rows.
        noun = "row" if len(row_list) == 1 else "rows"
        insufficient = (
            f"The query returned {len(row_list)} {noun}, but I couldn't summarise them "
            "reliably — see the result table."
        )

    tenant_str = str(tenant_id) if tenant_id is not None else ""
    conn_str = str(connection_id or "")
    scope_payload = {
        "tenant_id": tenant_str,
        "connection_id": conn_str,
        "filters": active_filters,
        "row_count": len(row_list),
        "sql_hash": hashlib.sha256((sql or "").encode()).hexdigest()[:16],
        "question": (question or "").strip(),
    }

    dump = base.model_dump()
    dump["insufficient_reason"] = insufficient
    dump["status"] = base.status
    return ChatInsightFacts(
        **dump,
        tenant_id=tenant_str,
        connection_id=conn_str,
        question=question,
        sql=sql or "",
        row_count=len(row_list),
        time_context=_time_context_from_plan(plan),
        scope_key=hashlib.sha256(json.dumps(scope_payload, sort_keys=True).encode()).hexdigest(),
        period_comparisons=period_comparisons,
        is_listing=is_listing_query,
        column_kinds=column_kinds,
        result_rows=_trim_rows(row_list),
    )


def chat_facts_fingerprint(facts: ChatInsightFacts) -> str:
    base = facts_fingerprint(facts)
    return hashlib.sha256(f"{base}:{facts.scope_key}".encode()).hexdigest()


def grounding_context_from_chat_facts(facts: ChatInsightFacts) -> GroundingContext:
    ctx = grounding_context_from_dashboard_facts(facts)
    ctx.tenant_id = facts.tenant_id
    ctx.connection_id = facts.connection_id
    ctx.active_filters = list(facts.active_filters)
    if facts.question:
        q_aliases = _aliases_from_label(facts.question)
        for claim in ctx.numeric_claims:
            claim.aliases.extend(a for a in q_aliases if a not in claim.aliases)
    if facts.period_comparisons:
        ctx.comparisons.extend(facts.period_comparisons)
    # Only what the facts don't already state: a duplicate claim for the
    # same value and entity makes the validator demand matching wording.
    known = {
        (round(c.value, 6), (c.dimension_value or "").strip().lower())
        for c in ctx.numeric_claims
    }
    for claim in _row_claims(facts.result_rows):
        key = (round(claim.value, 6), (claim.dimension_value or "").strip().lower())
        if key not in known:
            known.add(key)
            ctx.numeric_claims.append(claim)
    return ctx


def _row_claims(rows: list[dict[str, Any]]) -> list[NumericClaim]:
    """One claim per numeric cell, tied to its row's label and column, so the
    narrator may quote any value from the result table — but only against
    the entity and measure it actually belongs to."""
    claims: list[NumericClaim] = []
    for row in rows:
        text_keys = [
            k for k, v in row.items()
            if isinstance(v, str) and v.strip() and not _is_identifier_like_key(k)
        ]
        label = _composite_label(row, text_keys[:3]) or None
        for key, value in row.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if _is_identifier_like_key(key):
                continue
            metric = _humanize_key(key)
            claims.append(
                NumericClaim(
                    value=float(value),
                    metric=metric,
                    dimension_value=label,
                    aliases=_aliases_from_label(metric) + (_aliases_from_label(label) if label else []),
                )
            )
    return claims
