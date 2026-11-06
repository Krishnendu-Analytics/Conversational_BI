"""DuckDB-safe dashboard SQL for CSV/Excel file connections."""

from __future__ import annotations

import re
from typing import Any

from app.source_types import SourceType

_DURATION_STEMS = frozenset(
    {
        "talk",
        "idle",
        "hold",
        "wait",
        "handle",
        "ring",
        "wrap",
        "acw",
        "break",
        "pause",
        "mute",
    }
)
_CALENDAR_NAME_HINTS = (
    "created",
    "updated",
    "modified",
    "event",
    "start",
    "end",
    "login",
    "logout",
    "signed",
    "call_date",
    "order_date",
)
_DATE_TRUNC_OPEN = re.compile(r"DATE_TRUNC\s*\(", re.IGNORECASE)
_AGG_COL = re.compile(
    r"\b(SUM|AVG|MIN|MAX)\s*\(\s*(?:DISTINCT\s+)?((?:\"[^\"]+\"|[\w.]+))\s*\)",
    re.IGNORECASE,
)
_SQL_WS = re.compile(r"\s+")


def is_file_sql_engine(db_type: str | None) -> bool:
    key = (db_type or "").strip().lower()
    return SourceType.is_file_backed(key) or key == "duckdb"


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def quote_table_ref(qualified: str) -> str:
    return ".".join(quote_ident(part) for part in str(qualified).split(".") if part)


def sql_ident(name: str, db_type: str | None) -> str:
    if is_file_sql_engine(db_type):
        return quote_ident(name)
    return str(name)


def sql_table_ref(qualified: str, db_type: str | None) -> str:
    if is_file_sql_engine(db_type):
        return quote_table_ref(qualified)
    return str(qualified)


def looks_like_duration_name(name: str) -> bool:
    raw = str(name or "").strip().lower().replace(" ", "_")
    if not raw:
        return False
    if "duration" in raw:
        return True
    if any(hint in raw for hint in _CALENDAR_NAME_HINTS):
        return False
    stem = raw
    if stem.endswith("_time"):
        stem = stem[: -len("_time")]
    elif stem.endswith("time"):
        stem = stem[: -len("time")].rstrip("_")
    else:
        return False
    return stem in _DURATION_STEMS or any(
        stem.endswith(f"_{s}") or stem.startswith(f"{s}_") for s in _DURATION_STEMS
    )


def is_calendar_date_type(data_type: str | None) -> bool:
    dtype = str(data_type or "").lower().strip()
    if not dtype:
        return False
    if dtype in {
        "time",
        "timetz",
        "interval",
        "time without time zone",
        "time with time zone",
    }:
        return False
    if dtype.startswith("time ") and "stamp" not in dtype:
        return False
    return (
        "timestamp" in dtype
        or "datetime" in dtype
        or dtype == "date"
        or dtype.startswith("date")
    )


def is_calendar_date_column(col: dict[str, Any]) -> bool:
    name = str(col.get("name") or "")
    if looks_like_duration_name(name):
        return False
    return is_calendar_date_type(col.get("data_type"))


def duration_column_names(schema: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        for col in table.get("columns") or []:
            if not isinstance(col, dict):
                continue
            name = str(col.get("name") or "")
            if not name:
                continue
            dtype = str(col.get("data_type") or "").lower()
            if looks_like_duration_name(name) or (
                dtype in {"time", "timetz", "interval"} and "stamp" not in dtype
            ):
                names.add(name.lower())
    return names


def _split_first_comma_outside_parens(text: str) -> int | None:
    depth = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "," and depth == 0:
                return i
        i += 1
    return None


def wrap_date_trunc_for_duckdb(sql: str) -> str:
    """DATE_TRUNC('month', col) -> DATE_TRUNC('month', TRY_CAST(col AS TIMESTAMP))."""
    out: list[str] = []
    i = 0
    while True:
        match = _DATE_TRUNC_OPEN.search(sql, i)
        if not match:
            out.append(sql[i:])
            break
        out.append(sql[i : match.start()])
        start_inner = match.end()
        depth = 1
        j = start_inner
        while j < len(sql) and depth:
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
            j += 1
        inner = sql[start_inner : j - 1]
        comma = _split_first_comma_outside_parens(inner)
        if comma is None:
            out.append(sql[match.start() : j])
            i = j
            continue
        grain = inner[:comma].strip()
        expr = inner[comma + 1 :].strip()
        upper = expr.upper()
        if "TRY_CAST" in upper or upper.startswith("CAST("):
            out.append(sql[match.start() : j])
        else:
            out.append(f"DATE_TRUNC({grain}, TRY_CAST({expr} AS TIMESTAMP))")
        i = j
    return "".join(out)


def sql_buckets_duration(sql: str, duration_names: set[str]) -> bool:
    if not duration_names or "date_trunc" not in (sql or "").lower():
        return False
    lowered = sql.lower()
    for name in duration_names:
        if re.search(rf"date_trunc\s*\([^)]*\b{re.escape(name)}\b", lowered):
            return True
    return False


def sql_aggregates_duration(sql: str, duration_names: set[str]) -> bool:
    if not duration_names:
        return False
    for match in _AGG_COL.finditer(sql or ""):
        col = match.group(2).strip().strip('"').split(".")[-1].lower()
        if col in duration_names:
            return True
    return False


def normalize_sql_key(sql: str) -> str:
    return _SQL_WS.sub(" ", (sql or "").strip()).lower()


def dedupe_widgets_by_sql(widgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for widget in widgets:
        if not isinstance(widget, dict):
            continue
        key = normalize_sql_key(str(widget.get("sql") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(widget)
    return out


def rewrite_sql_for_file_engine(sql: str) -> str:
    return wrap_date_trunc_for_duckdb(sql or "")


def adapt_file_dashboard_spec(
    spec: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any] | None:
    """Rewrite/drop file-engine widgets that cannot run on DuckDB."""
    durations = duration_column_names(schema)
    widgets: list[dict[str, Any]] = []
    for widget in spec.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        sql = str(widget.get("sql") or "").strip()
        if not sql:
            continue
        if sql_aggregates_duration(sql, durations) or sql_buckets_duration(sql, durations):
            continue
        rewritten = rewrite_sql_for_file_engine(sql)
        item = dict(widget)
        item["sql"] = rewritten
        widgets.append(item)
    widgets = dedupe_widgets_by_sql(widgets)
    if not widgets:
        return None
    adapted = dict(spec)
    adapted["widgets"] = widgets
    return adapted


def duckdb_user_message(exc: BaseException) -> str:
    text = str(exc).split("\n")[0].strip()
    lowered = text.lower()
    if "conversion" in lowered or "cast" in lowered:
        return (
            "Could not convert a column to a date or number. "
            "Duration or text fields cannot be used in this chart."
        )
    if (
        "binder" in lowered
        or "does not exist" in lowered
        or "referenced column" in lowered
        or "not found" in lowered
    ):
        return "A column or table in this chart is not in the dataset."
    if "syntax" in lowered or "parser" in lowered:
        return "This chart used SQL that DuckDB could not parse."
    if text:
        return text[:220]
    return "The query could not be completed."
