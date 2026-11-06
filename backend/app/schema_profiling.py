"""Profile discovered tables: row counts, safe samples, column statistics."""

from __future__ import annotations

import re
from typing import Any

from app.config import get_settings
from app.serialize import json_safe
from app.query_execution import QueryCategory, limits_for_category
from app.source_types import SourceType

_MAX_SAMPLE_VALUES = 5
_MAX_CELL_LEN = 80
_MAX_STAT_COLUMNS = 40
_MAX_INTEGER_PAIR_COLUMNS = 6
_PII_HINTS = re.compile(
    r"(password|secret|token|ssn|social|credit|card|cvv|pin)",
    re.IGNORECASE,
)
_INTEGER_HINTS = ("int", "integer", "bigint", "smallint", "tinyint", "long")
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
_DATE_HINTS = ("date", "timestamp", "datetime")


def _safe_sample(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _PII_HINTS.search(text):
        return "[redacted]"
    if len(text) > _MAX_CELL_LEN:
        return text[:_MAX_CELL_LEN] + "…"
    return text


def _is_file_engine(db_type: str) -> bool:
    key = (db_type or "").lower()
    return SourceType.is_file_backed(key) or key == "duckdb"


def quote_profile_ident(name: str, db_type: str) -> str:
    key = (db_type or "").lower()
    raw = str(name or "")
    if key == "mysql":
        return f"`{raw.replace('`', '``')}`"
    if key == "mssql":
        return f"[{raw.replace(']', ']]')}]"
    return '"' + raw.replace('"', '""') + '"'


def qualified_table_name(
    schema_name: str | None,
    table_name: str,
    db_type: str = "postgres",
) -> str:
    """Quote a table reference for profiling SQL (DB and DuckDB)."""
    bare = str(table_name or "").strip()
    schema = (schema_name or "").strip()
    ident = quote_profile_ident
    if not schema or "." in bare:
        return ident(bare, db_type) if _is_file_engine(db_type) or db_type.lower() == "postgres" else bare
    if _is_file_engine(db_type):
        return f"{ident(schema, db_type)}.{ident(bare, db_type)}"
    key = (db_type or "").lower()
    if key == "mysql":
        return f"{ident(schema, db_type)}.{ident(bare, db_type)}"
    if key == "mssql":
        return f"{ident(schema, db_type)}.{ident(bare, db_type)}"
    if schema in {"public", "dbo"}:
        return ident(bare, db_type)
    return f"{ident(schema, db_type)}.{ident(bare, db_type)}"


def _qualified_name(schema_name: str | None, table_name: str) -> str:
    return qualified_table_name(schema_name, table_name, "postgres")


def _is_integer_type(dtype: str) -> bool:
    raw = (dtype or "").lower()
    return any(h in raw for h in _INTEGER_HINTS) and "interval" not in raw and "point" not in raw


def _is_numeric_type(dtype: str) -> bool:
    raw = (dtype or "").lower()
    if "bool" in raw:
        return False
    return any(h in raw for h in _NUMERIC_HINTS)


def _is_date_type(dtype: str) -> bool:
    raw = (dtype or "").lower()
    if raw in {"time", "timetz", "interval"} or (raw.startswith("time ") and "stamp" not in raw):
        return False
    return any(h in raw for h in _DATE_HINTS)


def _day_bucket_sql(col_ref: str, db_type: str) -> str:
    key = (db_type or "").lower()
    if _is_file_engine(key):
        return f"DATE_TRUNC('day', TRY_CAST({col_ref} AS TIMESTAMP))"
    if key == "postgres":
        return f"DATE_TRUNC('day', {col_ref})"
    if key == "mysql":
        return f"DATE({col_ref})"
    if key == "mssql":
        return f"CAST({col_ref} AS date)"
    return col_ref


def _row_get(row: dict[str, Any], *names: str) -> Any:
    if not isinstance(row, dict):
        return None
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_table_stats_sql(
    qualified: str,
    columns: list[dict[str, Any]],
    db_type: str,
) -> tuple[str, dict[str, Any]]:
    """One aggregation query: row count, distinct/null, min/max, date buckets, part/whole."""
    key = (db_type or "postgres").lower()
    select_parts = ["COUNT(*) AS __row_count"]
    alias_map: dict[str, Any] = {"columns": [], "pairs": []}
    usable: list[dict[str, Any]] = []
    for col in columns:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "").strip()
        if not name:
            continue
        usable.append(col)
        if len(usable) >= _MAX_STAT_COLUMNS:
            break

    for idx, col in enumerate(usable):
        name = str(col.get("name") or "").strip()
        dtype = str(col.get("data_type") or "")
        col_ref = quote_profile_ident(name, key)
        select_parts.append(f"COUNT(DISTINCT {col_ref}) AS d_{idx}")
        select_parts.append(
            f"SUM(CASE WHEN {col_ref} IS NULL THEN 1 ELSE 0 END) AS n_{idx}"
        )
        if _is_numeric_type(dtype) or _is_date_type(dtype):
            select_parts.append(f"MIN({col_ref}) AS min_{idx}")
            select_parts.append(f"MAX({col_ref}) AS max_{idx}")
        if _is_date_type(dtype):
            bucket = _day_bucket_sql(col_ref, key)
            select_parts.append(f"COUNT(DISTINCT {bucket}) AS db_{idx}")
        alias_map["columns"].append({"index": idx, "name": name, "data_type": dtype})

    int_cols = [
        (i, str(c.get("name") or ""), quote_profile_ident(str(c.get("name") or ""), key))
        for i, c in enumerate(usable)
        if _is_integer_type(str(c.get("data_type") or "")) and not c.get("is_pk")
    ][:_MAX_INTEGER_PAIR_COLUMNS]
    for i, (idx_a, name_a, ref_a) in enumerate(int_cols):
        select_parts.append(f"SUM({ref_a}) AS sum_{idx_a}")
        for idx_b, name_b, ref_b in int_cols[i + 1 :]:
            select_parts.append(
                f"SUM(CASE WHEN {ref_a} > {ref_b} THEN 1 ELSE 0 END) AS pw_{idx_a}_{idx_b}"
            )
            select_parts.append(
                f"SUM(CASE WHEN {ref_b} > {ref_a} THEN 1 ELSE 0 END) AS pw_{idx_b}_{idx_a}"
            )
            alias_map["pairs"].append(
                {"a_index": idx_a, "a_name": name_a, "b_index": idx_b, "b_name": name_b}
            )

    sql = f"SELECT {', '.join(select_parts)} FROM {qualified}"
    return sql, alias_map


def parse_table_stats_row(
    row: dict[str, Any] | None,
    alias_map: dict[str, Any],
) -> dict[str, Any]:
    """Shape a stats-query row into column_stats + part/whole pairs."""
    column_stats: dict[str, dict[str, Any]] = {}
    row_count = _as_int(_row_get(row or {}, "__row_count", "cnt")) if row else None
    if not row:
        return {
            "estimated_row_count": row_count,
            "column_stats": column_stats,
            "part_whole_pairs": [],
        }

    sums: dict[int, float] = {}
    for meta in alias_map.get("columns") or []:
        idx = int(meta["index"])
        name = str(meta["name"])
        dtype = str(meta.get("data_type") or "")
        distinct = _as_int(_row_get(row, f"d_{idx}"))
        nulls = _as_int(_row_get(row, f"n_{idx}"))
        stats: dict[str, Any] = {}
        if distinct is not None:
            stats["distinct_count"] = distinct
        if nulls is not None:
            stats["null_count"] = nulls
            if row_count and row_count > 0:
                stats["null_ratio"] = round(nulls / row_count, 6)
                stats["null_pct"] = round(100.0 * nulls / row_count, 2)
        min_v = _row_get(row, f"min_{idx}")
        max_v = _row_get(row, f"max_{idx}")
        if min_v is not None:
            stats["min"] = json_safe(min_v) if _is_numeric_type(dtype) else str(min_v)
        if max_v is not None:
            stats["max"] = json_safe(max_v) if _is_numeric_type(dtype) else str(max_v)
        if _is_date_type(dtype):
            if min_v is not None:
                stats["date_min"] = str(min_v)
            if max_v is not None:
                stats["date_max"] = str(max_v)
            buckets = _as_int(_row_get(row, f"db_{idx}"))
            if buckets is not None:
                stats["date_bucket_count"] = buckets
        sum_v = _row_get(row, f"sum_{idx}")
        if sum_v is not None:
            try:
                sums[idx] = float(sum_v)
            except (TypeError, ValueError):
                pass
        if stats:
            column_stats[name] = stats

    distincts: dict[int, int] = {}
    maxes: dict[int, float] = {}
    for meta in alias_map.get("columns") or []:
        idx = int(meta["index"])
        name = str(meta["name"])
        payload = column_stats.get(name) or {}
        dcount = _as_int(payload.get("distinct_count"))
        if dcount is not None:
            distincts[idx] = dcount
        try:
            if payload.get("max") is not None:
                maxes[idx] = float(payload["max"])
        except (TypeError, ValueError):
            pass

    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _measure_like(idx: int) -> bool:
        mx = maxes.get(idx)
        if mx is not None and mx >= 10_000:
            return False
        if not row_count:
            return True
        distinct = distincts.get(idx)
        if distinct is None:
            return True
        return (distinct / row_count) < 0.4

    for pair in alias_map.get("pairs") or []:
        idx_a = int(pair["a_index"])
        idx_b = int(pair["b_index"])
        if not _measure_like(idx_a) or not _measure_like(idx_b):
            continue
        name_a = str(pair["a_name"])
        name_b = str(pair["b_name"])
        viol_ab = _as_int(_row_get(row, f"pw_{idx_a}_{idx_b}"))
        viol_ba = _as_int(_row_get(row, f"pw_{idx_b}_{idx_a}"))
        sum_a = sums.get(idx_a)
        sum_b = sums.get(idx_b)
        candidates = (
            (name_a, name_b, viol_ab, sum_a, sum_b),
            (name_b, name_a, viol_ba, sum_b, sum_a),
        )
        for part, whole, violations, part_sum, whole_sum in candidates:
            if violations is None or violations > 0:
                continue
            if part_sum is None or whole_sum is None:
                continue
            if part_sum <= 0 or whole_sum <= part_sum:
                continue
            key = (part.lower(), whole.lower())
            if key in seen:
                continue
            seen.add(key)
            pairs.append({"part": part, "whole": whole})

    return {
        "estimated_row_count": row_count,
        "column_stats": column_stats,
        "part_whole_pairs": pairs,
    }


def merge_stats_into_table(table: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    """Write aggregation stats onto a schema table dict (in place)."""
    if parsed.get("estimated_row_count") is not None:
        table["estimated_row_count"] = parsed["estimated_row_count"]
        table["row_count"] = parsed["estimated_row_count"]
    profiling = table.get("profiling") if isinstance(table.get("profiling"), dict) else {}
    existing_stats = profiling.get("column_stats") if isinstance(profiling.get("column_stats"), dict) else {}
    merged_stats = {**existing_stats, **(parsed.get("column_stats") or {})}
    profiling["column_stats"] = merged_stats
    if parsed.get("part_whole_pairs"):
        profiling["part_whole_pairs"] = parsed["part_whole_pairs"]
    table["profiling"] = profiling
    col_stats = merged_stats
    for col in table.get("columns") or []:
        if not isinstance(col, dict):
            continue
        name = str(col.get("name") or "")
        payload = col_stats.get(name) or col_stats.get(name.lower())
        if not isinstance(payload, dict):
            continue
        if payload.get("distinct_count") is not None:
            col["distinct_count"] = payload["distinct_count"]
        if payload.get("null_count") is not None:
            col["null_count"] = payload["null_count"]
        if payload.get("null_ratio") is not None:
            col["null_ratio"] = payload["null_ratio"]
        if payload.get("date_bucket_count") is not None:
            col["date_bucket_count"] = payload["date_bucket_count"]
    return table


def profile_table(
    provider,
    *,
    db_type: str,
    schema_name: str | None,
    table_name: str,
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return row count + per-column samples and basic stats via aggregations."""
    db_type_lc = (db_type or "postgres").lower()
    qualified = qualified_table_name(schema_name, table_name, db_type_lc)
    limits = limits_for_category(QueryCategory.PROFILING)
    settings = get_settings()
    max_queries = settings.profiling_max_queries_per_table
    queries_run = 0

    def _run(sql: str) -> list[dict]:
        nonlocal queries_run
        if queries_run >= max_queries:
            return []
        queries_run += 1
        return provider.execute_read_only_query(
            sql,
            limits=limits,
            db_type=db_type_lc,
        )

    result: dict[str, Any] = {
        "estimated_row_count": None,
        "column_samples": {},
        "column_stats": {},
        "part_whole_pairs": [],
    }

    try:
        stats_sql, alias_map = build_table_stats_sql(qualified, columns, db_type_lc)
        rows = _run(stats_sql)
        parsed = parse_table_stats_row(rows[0] if rows else None, alias_map)
        result["estimated_row_count"] = parsed.get("estimated_row_count")
        result["column_stats"] = parsed.get("column_stats") or {}
        result["part_whole_pairs"] = parsed.get("part_whole_pairs") or []
    except Exception:  # noqa: BLE001
        try:
            rows = _run(f"SELECT COUNT(*) AS cnt FROM {qualified}")
            if rows:
                result["estimated_row_count"] = _as_int(_row_get(rows[0], "cnt", "__row_count"))
        except Exception:  # noqa: BLE001
            pass

    for col in columns:
        if not isinstance(col, dict):
            continue
        col_name = str(col.get("name") or "")
        if not col_name:
            continue
        col_ref = quote_profile_ident(col_name, db_type_lc)
        try:
            sample_sql = (
                f"SELECT DISTINCT {col_ref} AS v FROM {qualified} "
                f"WHERE {col_ref} IS NOT NULL LIMIT {_MAX_SAMPLE_VALUES}"
            )
            sample_rows = _run(sample_sql)
            samples = [
                s
                for s in (_safe_sample(r.get("v")) for r in sample_rows)
                if s is not None
            ]
            if samples:
                result["column_samples"][col_name] = samples
        except Exception:  # noqa: BLE001
            pass

        if col_name in result["column_stats"]:
            continue

        stats: dict[str, Any] = {}
        try:
            null_sql = (
                f"SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN {col_ref} IS NULL THEN 1 ELSE 0 END) AS nulls, "
                f"COUNT(DISTINCT {col_ref}) AS distincts "
                f"FROM {qualified}"
            )
            null_rows = _run(null_sql)
            if null_rows:
                total = _as_int(_row_get(null_rows[0], "total")) or 0
                nulls = _as_int(_row_get(null_rows[0], "nulls")) or 0
                distincts = _as_int(_row_get(null_rows[0], "distincts"))
                stats["null_count"] = nulls
                if distincts is not None:
                    stats["distinct_count"] = distincts
                if total > 0:
                    stats["null_ratio"] = round(nulls / total, 6)
                    stats["null_pct"] = round(100.0 * nulls / total, 2)
        except Exception:  # noqa: BLE001
            pass

        dtype = str(col.get("data_type") or "").lower()
        if _is_numeric_type(dtype) or _is_date_type(dtype):
            try:
                agg_sql = (
                    f"SELECT MIN({col_ref}) AS min_v, MAX({col_ref}) AS max_v "
                    f"FROM {qualified} WHERE {col_ref} IS NOT NULL"
                )
                agg_rows = _run(agg_sql)
                if agg_rows:
                    stats["min"] = json_safe(agg_rows[0].get("min_v"))
                    stats["max"] = json_safe(agg_rows[0].get("max_v"))
                    if _is_date_type(dtype):
                        stats["date_min"] = str(agg_rows[0].get("min_v"))
                        stats["date_max"] = str(agg_rows[0].get("max_v"))
            except Exception:  # noqa: BLE001
                pass

        if stats:
            result["column_stats"][col_name] = stats

    return result
