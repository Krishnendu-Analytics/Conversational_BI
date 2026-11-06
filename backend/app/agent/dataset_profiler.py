"""Generic dataset profiler for dashboard analytical planning.

Classifies discovered schema columns into dimensions, measures, rates,
durations, dates, identifiers, and candidate derived metrics.

Does not hard-code tenant- or industry-specific column names. Signals are
data type, name patterns, PK flags, and optional cardinality hints.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.measure_kind import is_code_like_name
from app.agent.file_dashboard_sql import (
    is_calendar_date_column,
    looks_like_duration_name,
    sql_ident,
    sql_table_ref,
)

logger = logging.getLogger(__name__)

MetricKind = Literal[
    "additive",
    "rate",
    "count",
    "duration",
    "distinct",
    "derived",
    "row_count",
]


class ProfileField(BaseModel):
    name: str
    column: str
    table: str
    data_type: str = ""
    expression: str
    kind: str
    score: int = 0
    cardinality: int | None = None
    distinct_count: int | None = None
    uniqueness_ratio: float | None = None
    null_count: int | None = None
    null_ratio: float | None = None
    min_value: Any = None
    max_value: Any = None
    date_min: str | None = None
    date_max: str | None = None
    date_bucket_count: int | None = None
    sample_values: list[str] = Field(default_factory=list)


class ProfileMetric(BaseModel):
    name: str
    column: str | None = None
    table: str | None = None
    expression: str
    kind: MetricKind
    aggregation: Literal["sum", "avg", "count", "count_distinct", "none"] = "sum"
    score: int = 0
    distinct_count: int | None = None
    null_count: int | None = None
    null_ratio: float | None = None
    min_value: Any = None
    max_value: Any = None
    # For derived KPIs: "percent" (0–100), "per_unit" or "ratio", and whether
    # a rise is good news ("higher_better") or bad ("lower_better").
    unit: str | None = None
    polarity: str | None = None
    # "semantic": defined in the tenant's semantic model (a business KPI with
    # its own name, shown as is); None: derived from the schema here.
    origin: str | None = None


class PartWholePair(BaseModel):
    part: str
    whole: str


class DatasetProfile(BaseModel):
    source_table: str | None = None
    row_count: int | None = None
    dimensions: list[ProfileField] = Field(default_factory=list)
    measures: list[ProfileMetric] = Field(default_factory=list)
    rates: list[ProfileMetric] = Field(default_factory=list)
    durations: list[ProfileField] = Field(default_factory=list)
    dates: list[ProfileField] = Field(default_factory=list)
    identifiers: list[ProfileField] = Field(default_factory=list)
    text_fields: list[ProfileField] = Field(default_factory=list)
    counts: list[ProfileMetric] = Field(default_factory=list)
    derived_metrics: list[ProfileMetric] = Field(default_factory=list)
    part_whole_pairs: list[PartWholePair] = Field(default_factory=list)
    stats_available: bool = False

    def all_metrics(self) -> list[ProfileMetric]:
        return [
            *self.measures,
            *self.rates,
            *self.counts,
            *self.derived_metrics,
        ]

    def metric_by_name(self, name: str) -> ProfileMetric | None:
        key = (name or "").strip().lower()
        for item in self.all_metrics():
            if item.name.lower() == key or (item.column or "").lower() == key:
                return item
        return None

    def dimension_by_name(self, name: str) -> ProfileField | None:
        key = (name or "").strip().lower()
        for item in [*self.dimensions, *self.dates]:
            if item.name.lower() == key or item.column.lower() == key:
                return item
        return None

    def catalog_names(self) -> dict[str, set[str]]:
        return {
            "metrics": {m.name.lower() for m in self.all_metrics()},
            "dimensions": {d.name.lower() for d in self.dimensions}
            | {d.column.lower() for d in self.dimensions},
            "dates": {d.name.lower() for d in self.dates}
            | {d.column.lower() for d in self.dates},
        }


_NUMERIC_HINTS = (
    "int",
    "numeric",
    "decimal",
    "float",
    "double",
    "real",
    "money",
    "number",
    "bigint",
    "smallint",
)
_RATE_PATTERNS = (
    re.compile(r"(^|_)(pct|percent|percentage)(_|$)"),
    re.compile(r"(^|_)(rate|ratio|utilization|share)(_|$)"),
    re.compile(r"_pct$"),
    re.compile(r"%"),
)
# Per-unit values (cost_per_mt, price, unit_cost, tariff, avg_*): summing
# them across rows is meaningless ("standard cost per MT = 47.5M").
_UNIT_RATE_PATTERNS = (
    re.compile(r"(^|_)per(_|$)"),
    re.compile(r"(^|_)(price|tariff|unit_cost|unit_price|unit_rate|avg|average|mean|yield|efficiency)(_|$)"),
)
# Quantity columns a per-unit value can be weighted by.
_QUANTITY_RE = re.compile(
    r"(^|_)(qty|quantity|volume|units|tonnes|tons|tonnage|mt|weight|kg|litres|liters|barrels|bbl)(_|$)"
)
_COUNT_TOKENS = (
    "count",
    "calls",
    "clicks",
    "impressions",
    "conversions",
    "visits",
    "hits",
    "orders",
    "records",
    "sessions",
    "leads",
)
_ADDITIVE_TOKENS = (
    "amount",
    "total",
    "sum",
    "value",
    "revenue",
    "sales",
    "spend",
    "cost",
    "balance",
    "outstanding",
    "volume",
    "fee",
    "calls",
    "clicks",
    "impressions",
    "conversions",
    "quantity",
    "qty",
    "units",
    # physical quantity units: "actual_production_mt" is a volume measure
    "mt",
    "kg",
    "tonnes",
    "tons",
    "bbl",
    "barrels",
    "litres",
    "liters",
)
_ENTITY_TOKENS = (
    "campaign",
    "customer",
    "product",
    "user",
    "agent",
    "client",
    "account",
    "sku",
    "employee",
    "vendor",
    "merchant",
    "store",
    "site",
)
_ATTRIBUTE_TOKENS = (
    "status",
    "type",
    "category",
    "region",
    "city",
    "segment",
    "channel",
    "source",
    "stage",
    "state",
    "country",
    "name",
)
_TEXT_ONLY_TOKENS = (
    "description",
    "comment",
    "notes",
    "note",
    "address",
    "email",
    "phone",
    "url",
    "path",
    "message",
    "body",
    "remark",
)
_RATIO_PAIRS: list[tuple[tuple[str, ...], tuple[str, ...], str]] = [
    (("click",), ("impression",), "Click-through rate"),
    (("conversion", "convert"), ("click",), "Conversion rate"),
    (("conversion", "convert"), ("impression",), "Conversion rate"),
    (("spend", "cost"), ("conversion", "convert"), "Cost per conversion"),
    (("revenue", "sales"), ("quantity", "qty", "unit"), "Revenue per unit"),
    (("revenue", "sales"), ("spend", "cost"), "Return on spend"),
]
_SYSTEM_TABLE_PREFIXES = ("auth_", "django_", "alembic_", "sqlite_")
_PERCENT_LIKE_LABEL_RE = re.compile(r"^\s*-?\d+(?:[.,]\d+)?\s*%\s*$")
_MEASURE_LIKE_LABEL_RATIO = 0.6


def is_percent_like_label(value: Any) -> bool:
    """True when a dimension label is itself a percentage, not a category name."""
    text = str(value or "").strip()
    return bool(text) and _PERCENT_LIKE_LABEL_RE.match(text) is not None


def values_are_measure_like_labels(values: list[Any] | None) -> bool:
    """Majority of labels look like percentages — not a useful grouping key."""
    labels = [str(v).strip() for v in (values or []) if v is not None and str(v).strip()]
    if len(labels) < 2:
        return False
    hits = sum(1 for label in labels if is_percent_like_label(label))
    return (hits / len(labels)) >= _MEASURE_LIKE_LABEL_RATIO


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def _humanize(name: str) -> str:
    raw = re.sub(r"[_\s]+", " ", (name or "").strip())
    raw = re.sub(r"%", " pct", raw)
    return raw.title() if raw else "Field"


def _is_numeric(col: dict[str, Any]) -> bool:
    if col.get("is_pk"):
        return False
    dtype = str(col.get("data_type") or "").lower()
    if "bool" in dtype:
        return False
    return any(h in dtype for h in _NUMERIC_HINTS)


def _is_text(col: dict[str, Any]) -> bool:
    dtype = str(col.get("data_type") or "").lower()
    return any(h in dtype for h in ("char", "text", "string", "varchar", "nvarchar", "uuid"))


_DATE_PART_NAMES = {
    "year", "quarter", "month", "week", "day",
    "calendar_year", "calendar_quarter", "calendar_month", "calendar_week", "calendar_day",
    "fiscal_year", "fiscal_quarter", "fiscal_month",
    "year_number", "month_number", "quarter_number", "week_number", "day_number",
    "day_of_week", "day_of_month", "day_of_year",
}


def _is_identifier(col: dict[str, Any]) -> bool:
    name = _normalize_name(str(col.get("name") or ""))
    dtype = str(col.get("data_type") or "").lower()
    if col.get("is_pk"):
        return True
    if (
        name in {"id", "pk", "uuid", "guid", "key"}
        or name.endswith("_id")
        or name.endswith("_uuid")
        # "_key" is the standard star-schema surrogate-key convention
        # (customer_key, product_key, ...) — as much an identifier as an
        # "_id" column, and must never compete with real measures/be
        # summed as if it were one.
        or name.endswith("_key")
        or name.endswith("_pk")
        # A numeric date-part (month number, calendar_year, ...) is not a
        # measure either — summing/breaking down by it produces nonsense
        # like "month by quarter: Q4 has 12".
        or name in _DATE_PART_NAMES
    ):
        return True
    if "uuid" in dtype or "guid" in dtype:
        return True
    if is_code_like_name(name):
        return True
    return False


def _name_has_token(name: str, tokens: tuple[str, ...]) -> bool:
    norm = f"_{_normalize_name(name)}_"
    return any(f"_{tok}_" in norm or norm.strip("_") == tok for tok in tokens)


# Point-in-time levels (stock on hand, balances): summing daily snapshots
# over a period is meaningless ("144M MT opening stock" over 24 months).
_SNAPSHOT_TOKENS = {"opening", "closing", "balance", "onhand", "available", "reserved", "stock"}
_SNAPSHOT_TABLE_TOKENS = {"inventory", "stock", "snapshot", "balance", "balances", "holding", "holdings"}
_FLOW_TOKENS = {"receipt", "receipts", "issue", "issues", "transfer", "adjustment", "purchase", "sales", "in", "out", "movement"}
# Category columns that are really dates ("customer_since", "joined_on").
_DATE_LIKE_NAME_TOKENS = {
    "since", "date", "dt", "timestamp", "datetime", "on", "at", "dob",
    # calendar parts belong on a time axis, not in a category breakdown
    "month", "quarter", "year", "week", "weekday", "fiscal",
}
# Fact-table text columns whose values are full dates, not categories. Unlike
# _DATE_LIKE_NAME_TOKENS this leaves calendar parts ("month" text) usable.
_TEXT_DATE_NAME_TOKENS = ("date", "dt", "timestamp", "datetime", "dob")
_PLAN_NAME_TOKENS = {"planned", "plan", "target", "budget", "budgeted", "standard", "forecast"}
_COST_NAME_TOKENS = {"cost", "costs", "expense", "expenses", "spend", "freight"}


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t}


def _is_snapshot_measure(column: str, table_name: str) -> bool:
    toks = _name_tokens(column)
    if toks & _SNAPSHOT_TOKENS and not toks & _FLOW_TOKENS:
        return True
    table_toks = _name_tokens(table_name)
    return bool(
        table_toks & _SNAPSHOT_TABLE_TOKENS
        and toks & {"value", "amount", "qty", "quantity"}
        and not toks & _FLOW_TOKENS
    )


def _snapshot_date_column(table: dict[str, Any]) -> str | None:
    """The fact table's own date column/key a snapshot is taken on."""
    candidates: list[tuple[int, str]] = []
    for col in table.get("columns") or []:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "")
        toks = _name_tokens(name)
        if is_calendar_date_column(col) or "date" in toks:
            # date / snapshot_date / date_key — a real period, never the
            # row's own surrogate key ("inventory_snapshot_key").
            candidates.append((0 if is_calendar_date_column(col) else 1, name))
    candidates.sort()
    return candidates[0][1] if candidates else None


def _subject_tokens(table_name: str) -> set[str]:
    bare = re.sub(r"^(fact|f|fct)_", "", (table_name or "").lower())
    return _name_tokens(bare) - {"fact", "data", "table", "tbl"}


def _is_rate_name(name: str) -> bool:
    raw = (name or "").strip().lower()
    return any(pat.search(raw) for pat in _RATE_PATTERNS) or _is_unit_rate_name(raw)


def _is_unit_rate_name(name: str) -> bool:
    raw = (name or "").strip().lower()
    return any(pat.search(raw) for pat in _UNIT_RATE_PATTERNS)


def _weighting_quantity_column(table: dict[str, Any], rate_column: str) -> str | None:
    """A quantity column in the same table to weight a per-unit value by —
    SUM(cost_per_mt * qty_mt) / SUM(qty_mt) is the true average cost per
    tonne; a plain AVG over rows would let a 1-tonne order count as much as
    a 10,000-tonne one."""
    for col in table.get("columns") or []:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "").strip()
        lowered = name.lower()
        if not name or lowered == rate_column.lower() or not _is_numeric(col):
            continue
        if _is_identifier(col) or _is_unit_rate_name(lowered) or _is_rate_name(lowered):
            continue
        if _QUANTITY_RE.search(lowered):
            return name
    return None


def _is_count_name(name: str) -> bool:
    return _name_has_token(name, _COUNT_TOKENS)


def _is_additive_name(name: str) -> bool:
    return _name_has_token(name, _ADDITIVE_TOKENS)


def _column_expr(
    table: dict[str, Any], column: str, db_type: str, *, qualify: bool = False
) -> str:
    if not qualify:
        # Column-only refs; the compiler FROM clause uses the profiled fact table.
        return sql_ident(column, db_type)
    # Qualified refs are needed once a dashboard spans more than one table
    # (a joined dimension table) — the compiler detects required tables and
    # builds JOINs from schema.table tokens in the expression text.
    schema_name = str(table.get("schema_name") or "").strip()
    bare_table = str(table.get("table_name") or "").strip()
    qualified_table = f"{schema_name}.{bare_table}" if schema_name else bare_table
    return sql_table_ref(f"{qualified_table}.{column}", db_type)


def _score_dimension(name: str, *, entity: bool) -> int:
    score = 12 if entity else 8
    if _name_has_token(name, _ATTRIBUTE_TOKENS):
        score += 10
    if _name_has_token(name, _ENTITY_TOKENS):
        score += 14
    if _name_has_token(name, _TEXT_ONLY_TOKENS):
        score -= 40
    return score


def _score_measure(name: str, kind: MetricKind) -> int:
    score = 10
    if kind == "rate":
        score += 8
    if _is_additive_name(name) or _is_count_name(name) or _is_rate_name(name):
        score += 16
    return score


def _pick_primary_table(schema: dict[str, Any], focus_text: str = "") -> dict[str, Any] | None:
    tables = [t for t in schema.get("tables") or [] if isinstance(t, dict)]
    if not tables:
        return None
    focus = set(re.findall(r"[a-z0-9]+", (focus_text or "").lower()))
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        bare = str(table.get("table_name") or "").lower()
        if any(bare.startswith(p) for p in _SYSTEM_TABLE_PREFIXES):
            continue
        columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
        numeric = sum(1 for c in columns if _is_numeric(c) and not _is_identifier(c))
        dates = sum(1 for c in columns if is_calendar_date_column(c))
        cats = sum(
            1
            for c in columns
            if _is_text(c) and not _is_identifier(c) and not looks_like_duration_name(str(c.get("name") or ""))
        )
        overlap = 0
        tokens = {t for t in re.split(r"[_\s]+", bare) if len(t) > 2}
        if focus:
            overlap = len(focus & tokens) * 60
        score = numeric * 4 + cats * 2 + dates * 3 + min(len(columns), 35) + overlap
        scored.append((score, table))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]
    return tables[0]


def pick_join_dimension_tables(
    schema: dict[str, Any],
    fact_table: dict[str, Any] | None,
    focus_text: str = "",
    *,
    max_tables: int = 3,
    max_hops: int = 2,
) -> list[dict[str, Any]]:
    """Tables reachable from the fact table by following FK references
    outward, worth joining in for readable breakdowns — e.g. orders ->
    products for "revenue by product", or fact_sales -> dim_sales_rep ->
    dim_territory for "sales by territory" when the fact table has no
    direct FK to the territory table.

    Traverses up to ``max_hops`` FK hops (fact -> dim -> sub-dim), not
    arbitrary joins across the whole schema, and only surfaces tables that
    add a genuinely descriptive column (not just more ids). The actual SQL
    JOIN path (including through any intermediate table) is computed later
    by the join planner from the real FK graph — this just decides which
    tables are worth offering as breakdown candidates in the first place.
    """
    _ = focus_text
    if fact_table is None:
        return []
    all_tables = [t for t in schema.get("tables") or [] if isinstance(t, dict)]

    def _resolve(ref_schema: str | None, ref_table: str) -> dict[str, Any] | None:
        for t in all_tables:
            if str(t.get("table_name") or "").lower() != ref_table.lower():
                continue
            t_schema = t.get("schema_name")
            if ref_schema and t_schema and str(t_schema).lower() != str(ref_schema).lower():
                continue
            return t
        return None

    def _table_key(t: dict[str, Any]) -> str:
        return f"{t.get('schema_name') or ''}.{t.get('table_name') or ''}".lower()

    def _usefulness(t: dict[str, Any]) -> int:
        columns = [c for c in (t.get("columns") or []) if isinstance(c, dict)]
        return sum(
            1
            for c in columns
            if _is_text(c)
            and not _is_identifier(c)
            and not looks_like_duration_name(str(c.get("name") or ""))
        )

    visited_keys = {_table_key(fact_table)}
    frontier = [fact_table]
    # (usefulness, closeness) so the sort below prefers more descriptive
    # tables first, and closer ones among equally descriptive ties.
    scored: list[tuple[int, int, dict[str, Any]]] = []
    calendar: dict[str, Any] | None = None

    for hop in range(1, max_hops + 1):
        next_frontier: list[dict[str, Any]] = []
        for current in frontier:
            for fk in current.get("foreign_keys") or []:
                if not isinstance(fk, dict):
                    continue
                ref_table_name = str(fk.get("referred_table") or "")
                if not ref_table_name:
                    continue
                dim_table = _resolve(fk.get("referred_schema"), ref_table_name)
                if dim_table is None:
                    continue
                key = _table_key(dim_table)
                if key in visited_keys:
                    continue  # already visited, or self/circular reference
                visited_keys.add(key)
                bare = str(dim_table.get("table_name") or "").lower()
                if any(bare.startswith(p) for p in _SYSTEM_TABLE_PREFIXES):
                    continue
                useful = _usefulness(dim_table)
                if useful > 0:
                    scored.append((useful, -hop, dim_table))
                if calendar is None and any(
                    isinstance(c, dict) and is_calendar_date_column(c)
                    for c in dim_table.get("columns") or []
                ):
                    calendar = dim_table
                next_frontier.append(dim_table)
        frontier = next_frontier
        if not frontier:
            break

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked = [t for _, _, t in scored[:max_tables]]
    # A calendar dimension (dim_date) has few descriptive text columns, so it
    # loses the ranking above to customer/product/territory — but without it
    # a star-schema fact keyed by date_key has no trend at all. Always add it.
    if calendar is not None and all(t is not calendar for t in picked):
        picked.append(calendar)
    return picked


def _find_measure_by_tokens(
    metrics: list[ProfileMetric],
    tokens: tuple[str, ...],
) -> ProfileMetric | None:
    for metric in metrics:
        hay = _normalize_name(metric.column or metric.name)
        if any(tok in hay for tok in tokens):
            return metric
    return None


def _build_derived_metrics(
    *,
    table: dict[str, Any],
    db_type: str,
    additive: list[ProfileMetric],
    dimensions: list[ProfileField],
) -> list[ProfileMetric]:
    derived: list[ProfileMetric] = []
    seen: set[str] = set()
    own_table = str(table.get("table_name") or "").lower().rsplit(".", 1)[-1]

    for field in dimensions:
        if not _name_has_token(field.column, _ENTITY_TOKENS):
            continue
        key = f"distinct:{field.column.lower()}"
        if key in seen:
            continue
        seen.add(key)
        # Distinct entities in the fact table are the ones that transacted
        # ("Active"); counted from a joined dimension table, the query reads
        # the whole master list, so calling it "Active" would overstate it.
        field_table = str(field.table or "").lower().rsplit(".", 1)[-1]
        prefix = "Active" if not field_table or field_table == own_table else "Total"
        derived.append(
            ProfileMetric(
                name=f"{prefix} {_humanize(field.column)}s",
                column=field.column,
                table=field.table,
                expression=f"COUNT(DISTINCT {field.expression})",
                kind="distinct",
                aggregation="count_distinct",
                score=field.score + 4,
            )
        )

    for numer_tokens, denom_tokens, label in _RATIO_PAIRS:
        numer = _find_measure_by_tokens(additive, numer_tokens)
        denom = _find_measure_by_tokens(additive, denom_tokens)
        if numer is None or denom is None or numer.expression == denom.expression:
            continue
        key = f"ratio:{numer.column}:{denom.column}"
        if key in seen:
            continue
        seen.add(key)
        derived.append(
            ProfileMetric(
                name=label,
                column=None,
                table=str(table.get("table_name") or "") or None,
                expression=(
                    f"({numer.expression}) * 1.0 / NULLIF(({denom.expression}), 0)"
                ),
                kind="derived",
                aggregation="none",
                score=min(numer.score, denom.score) + 6,
            )
        )
    return derived


def _build_part_whole_derived(
    *,
    additive: list[ProfileMetric],
    pairs: list[PartWholePair],
    db_type: str,
    table: dict[str, Any],
) -> list[ProfileMetric]:
    """Generic subset/total rates from profiled integer part/whole pairs."""
    _ = db_type
    by_col = {
        (m.column or "").lower(): m
        for m in additive
        if m.column
    }
    derived: list[ProfileMetric] = []
    seen: set[str] = set()
    table_name = str(table.get("table_name") or "") or None
    for pair in pairs:
        part = by_col.get(pair.part.lower())
        whole = by_col.get(pair.whole.lower())
        if part is None or whole is None or part.expression == whole.expression:
            continue
        key = f"pw:{part.column}:{whole.column}"
        if key in seen:
            continue
        seen.add(key)
        derived.append(
            ProfileMetric(
                name=f"{part.name} / {whole.name}",
                column=None,
                table=table_name,
                expression=(
                    f"({part.expression}) * 1.0 / NULLIF(({whole.expression}), 0)"
                ),
                kind="derived",
                aggregation="none",
                score=min(part.score, whole.score) + 10,
            )
        )
    return derived


# Semantic metrics outrank anything derived from the schema: they are the
# tenant's own KPI definitions. Confirmed ones lead; suggested ones (drafted
# by AI or discovery, not yet reviewed) still beat raw columns.
_SEMANTIC_SCORE = {"confirmed": 120, "suggested": 90}


def _bare_table(name: str | None) -> str:
    return str(name or "").split(".")[-1].strip().strip('"`[]').lower()


def _metric_belongs_to(
    item: dict[str, Any], table: str, columns: set[str] | None = None
) -> bool:
    """Does this semantic metric measure ``table``? By its recorded physical
    table, else by the table named in its formula, else — for a formula that
    names no table ("SUM(amount)") — by using this table's columns."""
    if not table:
        return True
    physical = _bare_table(item.get("physical_table"))
    if physical:
        return physical == table
    expr = str(item.get("expression") or "").lower()
    if re.search(rf"(?<![a-z0-9_]){re.escape(table)}\s*\.", expr):
        return True
    if re.search(r"[a-z_][a-z0-9_]*\s*\.\s*[a-z_]", expr):
        return False  # qualified with another table
    if columns is None:
        return True
    return bool(set(re.findall(r"[a-z_][a-z0-9_]*", expr)) & columns)


def _table_columns(table: dict[str, Any] | None) -> set[str]:
    return {
        str(c.get("name") or "").lower()
        for c in ((table or {}).get("columns") or [])
        if isinstance(c, dict)
    }


def _decimal_division(tree: Any, original: str) -> str:
    """``SUM(flag) / COUNT(*)`` divides two integers, and Postgres/SQL Server
    then drop the fraction (an on-time rate of 55% read as 0). Rewrite every
    ``a / b`` as ``1.0 * a / b`` so the division is decimal on any database;
    the value and its meaning are unchanged. Formulas without a division are
    returned untouched."""
    from sqlglot import exp

    divisions = list(tree.find_all(exp.Div))
    if not divisions:
        return original
    for div in divisions:
        div.set("this", exp.Mul(this=exp.Literal.number("1.0"), expression=div.this))
    projection = tree.expressions[0]
    node = projection.this if isinstance(projection, exp.Alias) else projection
    return node.sql() or original


def _semantic_metric(item: dict[str, Any], table: str | None) -> ProfileMetric | None:
    from app.agent.measure_kind import classify_expression

    name = str(item.get("name") or "").strip()
    expr = str(item.get("expression") or "").strip()
    status = item.get("status") or "confirmed"
    if not name or not expr or status not in _SEMANTIC_SCORE:
        return None
    try:
        import sqlglot

        tree = sqlglot.parse_one(f"SELECT {expr} AS m")
        kind_of = classify_expression(tree.expressions[0]) if tree.expressions else None
        expr = _decimal_division(tree, expr)
    except Exception:  # noqa: BLE001 — unparsable: treat as a plain total
        kind_of = None
    if str(item.get("format") or "").lower() in {"percent", "percentage", "pct", "%"}:
        kind_of = "percentage"
    score = _SEMANTIC_SCORE[status]
    common = {"name": name, "table": table, "expression": expr, "origin": "semantic"}
    if kind_of == "count":
        return ProfileMetric(kind="count", aggregation="count", score=score - 1, **common)
    if kind_of in {"percentage", "ratio"}:
        unit = "percent" if kind_of == "percentage" else "ratio"
        return ProfileMetric(kind="derived", aggregation="none", score=score, unit=unit, **common)
    if kind_of in {"average", "minimum", "maximum"}:
        return ProfileMetric(kind="rate", aggregation="avg", score=score, **common)
    return ProfileMetric(kind="additive", aggregation="sum", score=score, **common)


def merge_semantic_into_profile(
    profile: DatasetProfile,
    semantic: dict[str, Any] | None,
    table_columns: set[str] | None = None,
) -> DatasetProfile:
    """Put the tenant's semantic metrics (for this profile's table) first.

    Only metrics that measure this table are added — a whole-database
    dashboard builds one profile per table. Formulas are complete
    aggregates, so they are typed by structure: SUM -> measure, COUNT ->
    count, ratio/percentage -> business KPI, AVG -> rate."""
    if not isinstance(semantic, dict):
        return profile
    table = _bare_table(profile.source_table)
    existing_metrics = {m.name.lower() for m in profile.all_metrics()}
    existing_dims = {d.name.lower() for d in profile.dimensions}
    # Collected in the model's order, then put ahead of the schema-derived
    # candidates: the first metric defined for a table is its headline.
    added: dict[str, list[ProfileMetric]] = {"derived": [], "rate": [], "count": [], "additive": []}

    for item in semantic.get("metrics") or []:
        if not isinstance(item, dict) or not _metric_belongs_to(item, table, table_columns):
            continue
        metric = _semantic_metric(item, profile.source_table)
        if metric is None:
            continue
        key = metric.name.lower()
        if key in existing_metrics:
            # A schema-derived candidate of the same name ("Billed Amount"
            # from billed_amount) gives way to the tenant's definition; an
            # earlier semantic metric of that name wins over a later one.
            bucket_lists = (profile.measures, profile.rates, profile.counts, profile.derived_metrics)
            if any(m.name.lower() == key for b in added.values() for m in b):
                continue
            for bucket in bucket_lists:
                bucket[:] = [m for m in bucket if m.name.lower() != key]
        added[metric.kind if metric.kind in added else "additive"].append(metric)
        existing_metrics.add(metric.name.lower())

    # Confirmed before suggested, the model's order within each (stable sort).
    for kind, bucket in (
        ("derived", profile.derived_metrics),
        ("rate", profile.rates),
        ("count", profile.counts),
        ("additive", profile.measures),
    ):
        bucket[:0] = sorted(added[kind], key=lambda m: -m.score)

    for item in semantic.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        expr = str(item.get("expression") or "").strip()
        if not name or not expr or name.lower() in existing_dims:
            continue
        status = item.get("status")
        if status not in (None, "confirmed"):
            continue
        profile.dimensions.append(
            ProfileField(
                name=name,
                column=name,
                table=profile.source_table or "",
                expression=expr,
                kind="dimension",
                score=40,
            )
        )
        existing_dims.add(name.lower())
    return profile


def _as_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _as_ratio(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return round(min(1.0, number), 6) if number > 1 else round(number, 6)


def extract_schema_field_stats(
    schema: dict[str, Any],
    *,
    table_name: str | None = None,
) -> tuple[dict[str, dict[str, Any]], int | None, list[dict[str, str]]]:
    """Read cardinality / null / range hints already present on schema tables.

    Does not query the database. Missing stats stay absent.
    """
    stats_by_col: dict[str, dict[str, Any]] = {}
    row_count: int | None = None
    pairs: list[dict[str, str]] = []
    wanted = (table_name or "").strip().lower()
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("table_name") or "").strip().lower()
        if wanted and name and name != wanted:
            continue
        erc = _as_positive_int(
            table.get("estimated_row_count")
            if table.get("estimated_row_count") is not None
            else table.get("row_count")
        )
        if erc is not None:
            row_count = erc
        profiling = table.get("profiling") if isinstance(table.get("profiling"), dict) else {}
        raw_stats = profiling.get("column_stats") if isinstance(profiling, dict) else {}
        if isinstance(raw_stats, dict):
            for col_name, payload in raw_stats.items():
                if not isinstance(payload, dict) or not col_name:
                    continue
                stats_by_col[str(col_name).lower()] = dict(payload)
        raw_samples = profiling.get("column_samples") if isinstance(profiling, dict) else {}
        if isinstance(raw_samples, dict):
            for col_name, vals in raw_samples.items():
                if not col_name or not isinstance(vals, list):
                    continue
                dest = stats_by_col.setdefault(str(col_name).lower(), {})
                dest["samples"] = [str(v) for v in vals[:8] if v is not None and str(v).strip()]
        for item in profiling.get("part_whole_pairs") or []:
            if isinstance(item, dict) and item.get("part") and item.get("whole"):
                pairs.append({"part": str(item["part"]), "whole": str(item["whole"])})
        for col in table.get("columns") or []:
            if not isinstance(col, dict):
                continue
            col_name = str(col.get("name") or "").strip()
            if not col_name:
                continue
            dest = stats_by_col.setdefault(col_name.lower(), {})
            distinct = _as_positive_int(
                col.get("distinct_count")
                or col.get("cardinality")
                or col.get("n_unique")
                or dest.get("distinct_count")
                or dest.get("n_distinct")
                or dest.get("cardinality")
            )
            if distinct is not None:
                dest["distinct_count"] = distinct
            if col.get("null_count") is not None:
                dest["null_count"] = _as_positive_int(col.get("null_count"))
            if col.get("null_ratio") is not None:
                dest["null_ratio"] = _as_ratio(col.get("null_ratio"))
            if col.get("date_bucket_count") is not None:
                dest["date_bucket_count"] = _as_positive_int(col.get("date_bucket_count"))
            if col.get("date_min"):
                dest["date_min"] = str(col.get("date_min"))
            if col.get("date_max"):
                dest["date_max"] = str(col.get("date_max"))
            col_samples = col.get("sample_values") or col.get("samples")
            if isinstance(col_samples, list) and col_samples:
                dest["samples"] = [
                    str(v) for v in col_samples[:8] if v is not None and str(v).strip()
                ]
        if wanted:
            break
    return stats_by_col, row_count, pairs


def _uniqueness_ratio(cardinality: int | None, row_count: int | None) -> float | None:
    if cardinality is None or not row_count:
        return None
    return round(min(1.0, cardinality / row_count), 6)


def _column_stats(col: dict[str, Any], stats_by_col: dict[str, dict[str, Any]]) -> dict[str, Any]:
    name = str(col.get("name") or "").strip().lower()
    payload = dict(stats_by_col.get(name) or {})
    distinct = _as_positive_int(
        payload.get("distinct_count")
        or payload.get("n_distinct")
        or payload.get("cardinality")
        or col.get("distinct_count")
        or col.get("cardinality")
        or col.get("n_unique")
    )
    if distinct is not None:
        payload["distinct_count"] = distinct
    nulls = _as_positive_int(
        payload.get("null_count") if payload.get("null_count") is not None else col.get("null_count")
    )
    if nulls is not None:
        payload["null_count"] = nulls
    ratio = payload.get("null_ratio")
    if ratio is None and payload.get("null_pct") is not None:
        try:
            ratio = float(payload["null_pct"]) / 100.0
        except (TypeError, ValueError):
            ratio = None
    if ratio is not None:
        payload["null_ratio"] = _as_ratio(ratio)
    return payload


def _looks_like_high_uniqueness_identifier(
    *,
    uniqueness: float | None,
    cardinality: int | None,
) -> bool:
    """Structural identifier: nearly unique values, not a useful grouping."""
    if uniqueness is None or uniqueness < 0.8:
        return False
    if cardinality is not None and cardinality < 15:
        return False
    return True


def _field_from_stats(
    *,
    human: str,
    column: str,
    source: str,
    dtype: str,
    expr: str,
    kind: str,
    score: int,
    stats: dict[str, Any],
    row_count: int | None,
) -> ProfileField:
    card = _as_positive_int(stats.get("distinct_count"))
    uniq = _uniqueness_ratio(card, row_count)
    date_min = stats.get("date_min") or (
        str(stats.get("min")) if kind == "date" and stats.get("min") is not None else None
    )
    date_max = stats.get("date_max") or (
        str(stats.get("max")) if kind == "date" and stats.get("max") is not None else None
    )
    buckets = _as_positive_int(stats.get("date_bucket_count"))
    if kind == "date" and buckets is None:
        buckets = card
    raw_samples = stats.get("samples") or stats.get("sample_values") or []
    sample_values = [
        str(v)
        for v in (raw_samples if isinstance(raw_samples, list) else [])[:8]
        if v is not None and str(v).strip()
    ]
    return ProfileField(
        name=human,
        column=column,
        table=source,
        data_type=dtype,
        expression=expr,
        kind=kind,
        score=score,
        cardinality=card,
        distinct_count=card,
        uniqueness_ratio=uniq,
        null_count=_as_positive_int(stats.get("null_count")),
        null_ratio=_as_ratio(stats.get("null_ratio")) if stats.get("null_ratio") is not None else None,
        min_value=stats.get("min"),
        max_value=stats.get("max"),
        date_min=str(date_min) if date_min is not None else None,
        date_max=str(date_max) if date_max is not None else None,
        date_bucket_count=buckets,
        sample_values=sample_values,
    )


def _metric_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "distinct_count": _as_positive_int(stats.get("distinct_count")),
        "null_count": _as_positive_int(stats.get("null_count")),
        "null_ratio": _as_ratio(stats.get("null_ratio")) if stats.get("null_ratio") is not None else None,
        "min_value": stats.get("min"),
        "max_value": stats.get("max"),
    }


def profile_dataset(
    schema: dict[str, Any],
    *,
    db_type: str = "postgres",
    focus_text: str = "",
    semantic: dict[str, Any] | None = None,
    cardinalities: dict[str, int] | None = None,
    primary_table: dict[str, Any] | None = None,
    dimension_tables: list[dict[str, Any]] | None = None,
) -> DatasetProfile:
    """Inspect schema and return structured candidate analytics fields.

    ``primary_table``/``dimension_tables`` let a caller (dashboard building)
    pin the exact fact/dimension tables it already chose, so this function's
    own selection can't disagree. Left unset, both are derived here as before.
    """
    table = primary_table if primary_table is not None else _pick_primary_table(
        schema, focus_text=focus_text
    )
    if table is None:
        return DatasetProfile()

    join_tables = (
        dimension_tables
        if dimension_tables is not None
        else pick_join_dimension_tables(schema, table, focus_text)
    )
    # Only qualify expressions when a join is actually needed — keeps every
    # existing single-table dashboard byte-identical.
    qualify = bool(join_tables)

    source = str(table.get("table_name") or "")
    stats_by_col, row_count, extracted_pairs = extract_schema_field_stats(
        schema, table_name=source
    )
    for name, value in (cardinalities or {}).items():
        dest = stats_by_col.setdefault(name.lower(), {})
        dest["distinct_count"] = value
    if row_count is None:
        row_count = _as_positive_int(table.get("estimated_row_count") or table.get("row_count"))
    dimensions: list[ProfileField] = []
    measures: list[ProfileMetric] = []
    rates: list[ProfileMetric] = []
    durations: list[ProfileField] = []
    dates: list[ProfileField] = []
    identifiers: list[ProfileField] = []
    text_fields: list[ProfileField] = []
    counts: list[ProfileMetric] = []
    stats_available = bool(row_count) or any(
        payload.get("distinct_count") is not None for payload in stats_by_col.values()
    )
    part_whole_pairs = [
        PartWholePair(part=str(p["part"]), whole=str(p["whole"]))
        for p in extracted_pairs
        if p.get("part") and p.get("whole")
    ]

    for col in table.get("columns") or []:
        if not isinstance(col, dict):
            continue
        column = str(col.get("name") or "").strip()
        if not column:
            continue
        dtype = str(col.get("data_type") or "")
        expr = _column_expr(table, column, db_type, qualify=qualify)
        human = _humanize(column)
        stats = _column_stats(col, stats_by_col)
        card = _as_positive_int(stats.get("distinct_count"))
        uniq = _uniqueness_ratio(card, row_count)
        mstats = _metric_stats(stats)

        if _is_identifier(col) or (
            not _is_numeric(col)
            and not is_calendar_date_column(col)
            and _looks_like_high_uniqueness_identifier(uniqueness=uniq, cardinality=card)
        ):
            identifiers.append(
                _field_from_stats(
                    human=human,
                    column=column,
                    source=source,
                    dtype=dtype,
                    expr=expr,
                    kind="identifier",
                    score=0,
                    stats=stats,
                    row_count=row_count,
                )
            )
            continue

        if looks_like_duration_name(column):
            durations.append(
                _field_from_stats(
                    human=human,
                    column=column,
                    source=source,
                    dtype=dtype,
                    expr=expr,
                    kind="duration",
                    score=0,
                    stats=stats,
                    row_count=row_count,
                )
            )
            continue

        if is_calendar_date_column(col):
            dates.append(
                _field_from_stats(
                    human=human,
                    column=column,
                    source=source,
                    dtype=dtype,
                    expr=expr,
                    kind="date",
                    score=_score_dimension(column, entity=False) + 12,
                    stats=stats,
                    row_count=row_count,
                )
            )
            continue

        if _is_numeric(col) and _is_rate_name(column):
            rate_expression = f"AVG({expr})"
            weight_col = (
                _weighting_quantity_column(table, column) if _is_unit_rate_name(column) else None
            )
            if weight_col:
                weight_expr = _column_expr(table, weight_col, db_type, qualify=qualify)
                rate_expression = (
                    f"SUM({expr} * {weight_expr}) / NULLIF(SUM({weight_expr}), 0)"
                )
            rates.append(
                ProfileMetric(
                    name=human,
                    column=column,
                    table=source,
                    expression=rate_expression,
                    kind="rate",
                    aggregation="avg",
                    score=_score_measure(column, "rate"),
                    **mstats,
                )
            )
            continue

        if _is_numeric(col) and _is_snapshot_measure(column, source):
            snap_date = _snapshot_date_column(table)
            if snap_date:
                date_expr = _column_expr(table, snap_date, db_type, qualify=qualify)
                rates.append(
                    ProfileMetric(
                        name=f"Average {human}",
                        column=column,
                        table=source,
                        # Average level per snapshot date: correct for any
                        # period or grouping, unlike a sum across dates.
                        expression=f"SUM({expr}) * 1.0 / NULLIF(COUNT(DISTINCT {date_expr}), 0)",
                        kind="rate",
                        aggregation="avg",
                        score=_score_measure(column, "additive"),
                        **mstats,
                    )
                )
                continue

        if _is_numeric(col):
            # Remaining numerics are additive; identifiers/rates/durations already skipped.
            subject_bonus = 0
            col_toks = _name_tokens(column)
            if col_toks & _subject_tokens(source):
                # The volume/value the table is about ("actual_production_mt"
                # in fact_production) makes the best headline; its cost less so.
                subject_bonus = 12 - (8 if col_toks & _COST_NAME_TOKENS else 0)
            if col_toks & _PLAN_NAME_TOKENS:
                # Plans and targets are comparison baselines, not headlines.
                subject_bonus -= 20
            metric = ProfileMetric(
                name=human,
                column=column,
                table=source,
                expression=f"SUM({expr})",
                kind="count" if _is_count_name(column) else "additive",
                aggregation="sum",
                score=_score_measure(column, "count" if _is_count_name(column) else "additive") + subject_bonus,
                **mstats,
            )
            if metric.kind == "count":
                counts.append(metric)
            else:
                measures.append(metric)
            continue

        if _is_text(col) or str(col.get("data_type") or "").lower() in {"", "unknown"}:
            if _name_has_token(column, _TEXT_ONLY_TOKENS):
                text_fields.append(
                    _field_from_stats(
                        human=human,
                        column=column,
                        source=source,
                        dtype=dtype,
                        expr=expr,
                        kind="text",
                        score=0,
                        stats=stats,
                        row_count=row_count,
                    )
                )
                continue
            # A date stored as text (e.g. varchar invoice_date) has one value
            # per day: as a category it yields a meaningless ranking of
            # individual days instead of a trend.
            if _name_has_token(column, _TEXT_DATE_NAME_TOKENS):
                text_fields.append(
                    _field_from_stats(
                        human=human,
                        column=column,
                        source=source,
                        dtype=dtype,
                        expr=expr,
                        kind="text",
                        score=0,
                        stats=stats,
                        row_count=row_count,
                    )
                )
                continue
            entity = _name_has_token(column, _ENTITY_TOKENS)
            attr = _name_has_token(column, _ATTRIBUTE_TOKENS) or _is_text(col)
            samples = stats.get("samples") or []
            if values_are_measure_like_labels(samples if isinstance(samples, list) else []):
                text_fields.append(
                    _field_from_stats(
                        human=human,
                        column=column,
                        source=source,
                        dtype=dtype,
                        expr=expr,
                        kind="measure_like",
                        score=0,
                        stats=stats,
                        row_count=row_count,
                    )
                )
                continue
            if entity or attr or _is_text(col):
                dimensions.append(
                    _field_from_stats(
                        human=human,
                        column=column,
                        source=source,
                        dtype=dtype,
                        expr=expr,
                        kind="dimension",
                        score=_score_dimension(column, entity=entity),
                        stats=stats,
                        row_count=row_count,
                    )
                )
            else:
                text_fields.append(
                    _field_from_stats(
                        human=human,
                        column=column,
                        source=source,
                        dtype=dtype,
                        expr=expr,
                        kind="text",
                        score=0,
                        stats=stats,
                        row_count=row_count,
                    )
                )

    for dim_table in join_tables:
        dim_source = str(dim_table.get("table_name") or "")
        dim_stats_by_col, dim_row_count, _ = extract_schema_field_stats(
            schema, table_name=dim_source
        )
        bare_source = re.sub(r"^(dim|fact|dimension|lookup|ref)_", "", dim_source.lower())
        table_label = _humanize(bare_source).rstrip("s") or dim_source
        for col in dim_table.get("columns") or []:
            if not isinstance(col, dict):
                continue
            column = str(col.get("name") or "").strip()
            if not column:
                continue
            if is_calendar_date_column(col):
                # A star-schema fact table references its date through an
                # integer surrogate key (date_key), not a real date column
                # on the fact table itself — the actual calendar date only
                # exists on the joined dim_date/dim_calendar table. Without
                # this branch, that's the *only* place a trend candidate
                # could ever come from, and it was being silently dropped
                # (this loop only ever fed `dimensions`, never `dates`),
                # making a trend chart structurally unreachable for any
                # fact table that follows this extremely common pattern.
                expr = _column_expr(dim_table, column, db_type, qualify=True)
                stats = _column_stats(col, dim_stats_by_col)
                dates.append(
                    _field_from_stats(
                        human=_humanize(column),
                        column=column,
                        source=dim_source,
                        dtype=str(col.get("data_type") or ""),
                        expr=expr,
                        kind="date",
                        score=_score_dimension(column, entity=False) + 12,
                        stats=stats,
                        row_count=dim_row_count,
                    )
                )
                continue
            if _is_identifier(col) or _is_numeric(col):
                continue
            if not _is_text(col) or looks_like_duration_name(column):
                continue
            if _name_tokens(column) & _DATE_LIKE_NAME_TOKENS:
                continue
            human = _humanize(column)
            label = (
                human
                if column.lower().startswith(table_label.lower())
                else f"{table_label} {human}".strip()
            )
            expr = _column_expr(dim_table, column, db_type, qualify=True)
            stats = _column_stats(col, dim_stats_by_col)
            dimensions.append(
                _field_from_stats(
                    human=label,
                    column=column,
                    source=dim_source,
                    dtype=str(col.get("data_type") or ""),
                    expr=expr,
                    kind="dimension",
                    score=_score_dimension(column, entity=True),
                    stats=stats,
                    row_count=dim_row_count,
                )
            )

    measures.sort(key=lambda m: m.score, reverse=True)
    counts.sort(key=lambda m: m.score, reverse=True)
    rates.sort(key=lambda m: m.score, reverse=True)
    dimensions.sort(key=lambda d: d.score, reverse=True)
    dates.sort(key=lambda d: d.score, reverse=True)

    additive_for_ratios = [*counts, *measures]
    derived = _build_derived_metrics(
        table=table,
        db_type=db_type,
        additive=additive_for_ratios,
        dimensions=dimensions,
    )
    derived.extend(
        _build_part_whole_derived(
            additive=additive_for_ratios,
            pairs=part_whole_pairs,
            db_type=db_type,
            table=table,
        )
    )
    counts.append(
        ProfileMetric(
            name="Record Count",
            expression="COUNT(*)",
            kind="row_count",
            aggregation="count",
            score=0,
        )
    )

    profile = DatasetProfile(
        source_table=source or None,
        row_count=row_count,
        dimensions=dimensions,
        measures=measures,
        rates=rates,
        durations=durations,
        dates=dates,
        identifiers=identifiers,
        text_fields=text_fields,
        counts=counts,
        derived_metrics=derived,
        part_whole_pairs=part_whole_pairs,
        stats_available=stats_available,
    )
    return merge_semantic_into_profile(profile, semantic, _table_columns(table))


def profile_to_semantic_catalog(profile: DatasetProfile) -> dict[str, Any]:
    """Shape a DatasetProfile as the compiler's semantic catalog."""
    metrics = [
        {"name": m.name, "expression": m.expression, "status": "confirmed"}
        for m in profile.all_metrics()
    ]
    dimensions = [
        {"name": d.name, "expression": d.expression, "status": "confirmed"}
        for d in (*profile.dimensions, *profile.dates)
    ]
    return {
        "metrics": metrics,
        "dimensions": dimensions,
        "relationships": [],
    }
