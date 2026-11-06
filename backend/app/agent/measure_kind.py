"""Classify result columns as additive or non-additive measures.

Narration and charts must never sum a percentage, show a ratio as a
"share of total", or roll up an average — but result columns carry whatever
alias the SQL generator picked, so the column name alone is unreliable
("gpm", "oee", "grossMarginPct", a non-English alias...).

Kinds are resolved per result column, strongest signal first:

1. An explicit percentage format on a matching semantic-layer metric.
2. The structure of the SQL expression that actually produced the column
   (``SUM(a) / NULLIF(SUM(b), 0) * 100`` is a percentage whatever it is
   called, across any number of joins, CTEs or subqueries).
3. The matching semantic metric's own expression.
4. Name tokens (``*_pct``, ``*_margin``, ``avg_*`` ...) as a last resort.

Kinds: ``amount`` and ``count`` are additive; ``percentage``, ``ratio``,
``average``, ``minimum``, ``maximum``, ``running_total`` and ``rank`` are not.
A ``rank`` (RANK(), ROW_NUMBER(), ``sales_rank``) is a position where 1 is
best: never a total, a share, or "highest is X at 4".
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

ADDITIVE_KINDS = frozenset({"amount", "count"})
NON_ADDITIVE_KINDS = frozenset(
    {"percentage", "ratio", "average", "minimum", "maximum", "running_total", "rank"}
)

_PERCENT_FORMATS = frozenset({"percent", "percentage", "pct", "%"})

# Matched as whole underscore-separated name tokens, so "duration" is not
# read as "ratio" nor "operated_hours" as "rate".
_NAME_PERCENT_RE = re.compile(
    r"(?:^|_)(pct|percent|percentage|margin|share|contribution)(?:_|$)", re.IGNORECASE
)
_NAME_RATIO_RE = re.compile(r"(?:^|_)(ratio|rate|yield|efficiency)(?:_|$)", re.IGNORECASE)
_NAME_AVG_RE = re.compile(r"(?:^|_)(avg|average|mean)(?:_|$)", re.IGNORECASE)
# Per-unit values (cost_per_mt, unit_price, tariff): even when a query
# SUMs one, the result is not a shareable total.
_NAME_UNIT_RATE_RE = re.compile(
    r"(?:^|_)(per|price|tariff|unit_cost|unit_price|unit_rate)(?:_|$)", re.IGNORECASE
)
_NAME_RANK_RE = re.compile(r"(?:^|_)(rank|ranking|rnk)(?:_|$)", re.IGNORECASE)
def _optional(*names: str) -> tuple[type, ...]:
    """Expression classes that exist in the installed sqlglot. The deployed
    image pins an older sqlglot (15.x) that parses NULLIF, MEDIAN, RANK...
    as generic Anonymous functions; referencing a missing class made every
    classification fail there."""
    return tuple(cls for cls in (getattr(exp, n, None) for n in names) if cls is not None)


_RANK_FUNCTION_NAMES = frozenset({"rank", "dense_rank", "row_number", "ntile", "percent_rank", "cume_dist"})
_RANK_FUNCTIONS = _optional("Rank", "DenseRank", "RowNumber", "Ntile", "PercentRank", "CumeDist")


def _is_rank_function(node: exp.Expression) -> bool:
    if isinstance(node, _RANK_FUNCTIONS):
        return True
    return isinstance(node, exp.Anonymous) and str(node.name).lower() in _RANK_FUNCTION_NAMES
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Tried in order when the platform dialect is not known at narration time.
_FALLBACK_DIALECTS: tuple[str | None, ...] = (
    None, "postgres", "tsql", "mysql", "snowflake", "bigquery", "oracle", "duckdb",
)


def normalize_name(name: str) -> str:
    """snake_case, lower-cased key: "grossMarginPct" / "Gross Margin %" -> gross_margin_pct."""
    text = _CAMEL_RE.sub("_", str(name or "").strip())
    text = text.replace("%", "_pct")
    text = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")
    return text.lower()


# Numbers that are really codes: uploaded CSV/Excel files type a phone or
# PIN column as BIGINT, and "total phone by patient" is nonsense.
_NAME_CODE_RE = re.compile(
    r"(?:^|_)(phone|phones|mobile|telephone|tel|fax|whatsapp|contact_no|contact_number"
    r"|zip|zipcode|zip_code|postcode|postal_code|pincode|pin_code"
    r"|ssn|aadhaar|aadhar|passport_no|passport_number|account_no|account_number"
    r"|card_no|card_number|iban|ifsc|isbn|barcode|ean|upc|imei|serial_no|serial_number)(?:_|$)",
    re.IGNORECASE,
)


def is_code_like_name(name: str) -> bool:
    """A numeric column that identifies or contacts rather than measures
    (phone, zip/PIN, account/card/serial numbers): never sum or average it."""
    return bool(_NAME_CODE_RE.search(normalize_name(name)))


def kind_from_name(name: str) -> str | None:
    key = normalize_name(name)
    if _NAME_RANK_RE.search(key):
        return "rank"
    if _NAME_PERCENT_RE.search(key):
        return "percentage"
    if _NAME_RATIO_RE.search(key):
        return "ratio"
    if _NAME_AVG_RE.search(key):
        return "average"
    if _NAME_UNIT_RATE_RE.search(key):
        return "ratio"
    return None


def _numeric_literal(node: exp.Expression) -> float | None:
    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast)):
        node = node.this
    if isinstance(node, exp.Neg):
        inner = _numeric_literal(node.this)
        return -inner if inner is not None else None
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.this)
        except (TypeError, ValueError):
            return None
    return None


def _scaled_by_100(node: exp.Expression) -> bool:
    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast)):
        node = node.this
    if not isinstance(node, exp.Mul):
        return False
    return 100 in (_numeric_literal(node.this), _numeric_literal(node.expression))


def classify_expression(
    node: exp.Expression | None,
    alias_kinds: dict[str, str] | None = None,
) -> str | None:
    """Kind of one SELECT expression, or None when it cannot be told."""
    if node is None:
        return None
    aliases = alias_kinds or {}

    # Wrappers that do not change what the value measures.
    if isinstance(node, (exp.Alias, exp.Paren, exp.Cast, exp.TryCast, exp.Neg, exp.Abs)):
        return classify_expression(node.this, aliases)
    if isinstance(node, (exp.Round, exp.Coalesce, exp.Floor, exp.Ceil, *_optional("Nullif"))):
        return classify_expression(node.this, aliases)
    if isinstance(node, exp.Anonymous) and str(node.name).lower() == "nullif" and node.expressions:
        return classify_expression(node.expressions[0], aliases)

    if isinstance(node, exp.Window):
        inner = node.this
        if _is_rank_function(inner):
            return "rank"
        if node.args.get("order") is not None and isinstance(inner, (exp.Sum, exp.Count)):
            return "running_total"
        return classify_expression(inner, aliases)

    if isinstance(node, exp.Count):
        return "count"
    if isinstance(node, exp.Sum):
        return "amount"
    if isinstance(node, (exp.Avg, *_optional("Median"))) or (
        isinstance(node, exp.Anonymous) and str(node.name).lower() in {"median", "mean"}
    ):
        return "average"
    if isinstance(node, exp.Min):
        return "minimum"
    if isinstance(node, exp.Max):
        return "maximum"

    if isinstance(node, exp.Div):
        denominator = _numeric_literal(node.expression)
        if denominator is not None:
            # Unit scaling (SUM(x) / 1000) keeps the numerator's kind.
            return classify_expression(node.this, aliases)
        # 100 * SUM(a) / SUM(b) parses as (100 * SUM(a)) / SUM(b).
        return "percentage" if _scaled_by_100(node.this) else "ratio"

    if isinstance(node, exp.Mul):
        left, right = node.this, node.expression
        left_lit, right_lit = _numeric_literal(left), _numeric_literal(right)
        if left_lit is not None or right_lit is not None:
            factor = left_lit if left_lit is not None else right_lit
            other = right if left_lit is not None else left
            other_kind = classify_expression(other, aliases)
            if other_kind == "ratio" and factor == 100:
                return "percentage"
            return other_kind
        kinds = {classify_expression(left, aliases), classify_expression(right, aliases)}
        if "ratio" in kinds or "percentage" in kinds:
            # e.g. SUM(sales) * AVG(margin_ratio) — an amount scaled by a rate.
            other = kinds - {"ratio", "percentage"}
            return "amount" if other <= ADDITIVE_KINDS and other else None
        return None

    if isinstance(node, (exp.Add, exp.Sub)):
        kinds = {classify_expression(node.this, aliases), classify_expression(node.expression, aliases)}
        kinds.discard(None)
        if kinds and kinds <= ADDITIVE_KINDS:
            # SUM(revenue) - SUM(cost) is still additive across groups.
            return "amount"
        if len(kinds) == 1:
            return next(iter(kinds))
        return None

    if isinstance(node, exp.Case):
        branches = [ifs.args.get("true") for ifs in node.args.get("ifs") or []]
        if node.args.get("default") is not None:
            branches.append(node.args["default"])
        kinds = {classify_expression(b, aliases) for b in branches if _numeric_literal(b) is None}
        kinds.discard(None)
        return next(iter(kinds)) if len(kinds) == 1 else None

    if isinstance(node, exp.Column):
        # A column selected from a CTE/subquery inherits the kind of the
        # inner expression that defined it.
        return aliases.get(normalize_name(node.name))

    return None


def _parse(sql: str) -> exp.Expression | None:
    for dialect in _FALLBACK_DIALECTS:
        try:
            tree = sqlglot.parse_one(sql, read=dialect) if dialect else sqlglot.parse_one(sql)
        except Exception:  # noqa: BLE001
            continue
        if tree is not None:
            return tree
    return None


def classify_sql_columns(sql: str | None) -> dict[str, str]:
    """Map each output column (normalized name) of a SELECT to its kind."""
    if not sql or not sql.strip():
        return {}
    tree = _parse(sql)
    if tree is None:
        return {}

    selects = list(tree.find_all(exp.Select))
    if not selects:
        return {}
    # Innermost first, so outer selects can resolve columns defined inside
    # CTEs and subqueries through their aliases.
    selects.sort(key=lambda s: s.depth, reverse=True)
    alias_kinds: dict[str, str] = {}
    for select in selects:
        for projection in select.expressions:
            name = projection.alias_or_name
            if not name:
                continue
            kind = classify_expression(projection, alias_kinds)
            if kind:
                alias_kinds.setdefault(normalize_name(name), kind)

    outer = tree
    while isinstance(outer, (exp.Union, exp.Subquery)):
        outer = outer.this if isinstance(outer, exp.Subquery) else outer.left
    if not isinstance(outer, exp.Select):
        outer = selects[-1]

    result: dict[str, str] = {}
    for projection in outer.expressions:
        name = projection.alias_or_name
        if not name:
            continue
        kind = classify_expression(projection, alias_kinds)
        if kind:
            result[normalize_name(name)] = kind
    return result


def _semantic_metric_kinds(semantic_catalog: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    """(explicit percentage-format kinds, expression-derived kinds) by name key."""
    explicit: dict[str, str] = {}
    derived: dict[str, str] = {}
    if not isinstance(semantic_catalog, dict):
        return explicit, derived
    for metric in semantic_catalog.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        keys = {
            normalize_name(v)
            for v in [metric.get("name"), metric.get("label"), *(metric.get("synonyms") or [])]
            if v
        }
        keys.discard("")
        if not keys:
            continue
        fmt = str(metric.get("format") or "").strip().lower()
        expr_kind: str | None = None
        expression = str(metric.get("expression") or "").strip()
        if expression:
            tree = _parse(f"SELECT {expression} AS m")
            if isinstance(tree, exp.Select) and tree.expressions:
                expr_kind = classify_expression(tree.expressions[0])
        for key in keys:
            if fmt in _PERCENT_FORMATS:
                explicit[key] = "percentage"
            if expr_kind:
                derived.setdefault(key, expr_kind)
    return explicit, derived


def resolve_column_kinds(
    columns: list[str],
    *,
    sql: str | None = None,
    semantic_catalog: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Kind per result column (keyed by the column's own name)."""
    try:
        sql_kinds = classify_sql_columns(sql)
    except Exception:  # noqa: BLE001
        logger.info("measure_kind_sql_classification_failed")
        sql_kinds = {}
    try:
        explicit, derived = _semantic_metric_kinds(semantic_catalog)
    except Exception:  # noqa: BLE001
        logger.info("measure_kind_semantic_classification_failed")
        explicit, derived = {}, {}

    out: dict[str, str] = {}
    for column in columns:
        key = normalize_name(column)
        kind = explicit.get(key) or sql_kinds.get(key) or derived.get(key) or kind_from_name(column)
        if kind in ADDITIVE_KINDS and _NAME_UNIT_RATE_RE.search(key):
            # SUM(cost_per_mt) runs, but its "total" and shares are meaningless.
            kind = "ratio"
        if kind:
            out[column] = kind
    return out
