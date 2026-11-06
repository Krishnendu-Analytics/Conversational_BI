"""Pick a real header row from messy spreadsheets (title rows, blank cells)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_HEADER_SCAN_ROWS = 15
_PLACEHOLDER_COL_RE = re.compile(r"^(column|unnamed|col)(_?\d+)?$", re.IGNORECASE)
_SQL_TOKENS = frozenset(
    {
        "select",
        "from",
        "where",
        "and",
        "or",
        "not",
        "as",
        "on",
        "join",
        "left",
        "right",
        "inner",
        "outer",
        "group",
        "by",
        "order",
        "limit",
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "distinct",
        "case",
        "when",
        "then",
        "else",
        "end",
        "null",
        "cast",
        "coalesce",
    }
)
_COUNT_STAR_RE = re.compile(r"^\s*count\s*\(\s*(?:\*|1)\s*\)\s*$", re.IGNORECASE)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_placeholder_column_name(name: str | None) -> bool:
    text = (name or "").strip()
    if not text:
        return True
    return bool(_PLACEHOLDER_COL_RE.match(text.replace(" ", "_")))


def _looks_numeric(text: str) -> bool:
    if not text:
        return False
    cleaned = text.replace(",", "").replace("%", "").replace("$", "").strip()
    if re.fullmatch(r"[-+]?\d+(\.\d+)?", cleaned):
        return True
    return False


def _looks_date(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text)
        or re.fullmatch(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", text)
    )


def score_header_row(row: Sequence[Any] | None) -> float:
    """Higher is a better header. Negative means 'title / empty / data'."""
    if not row:
        return -1.0
    filled = [cell_text(v) for v in row if cell_text(v)]
    if not filled:
        return -1.0
    width = len(row)
    # Banner/title rows: one label across a wide sheet.
    if len(filled) < 2 and width > 3:
        return -1.0

    unique = {v.lower() for v in filled}
    numeric = sum(1 for v in filled if _looks_numeric(v) or _looks_date(v))
    score = float(len(filled) * 2 + len(unique))
    score -= numeric * 2.5
    if len(unique) < max(1, len(filled) * 0.6):
        score -= 4
    fill_ratio = len(filled) / max(width, 1)
    score += fill_ratio * 3
    return score


def _next_row_looks_like_data(header: Sequence[Any], nxt: Sequence[Any] | None) -> bool:
    if not nxt:
        return False
    header_filled = [cell_text(v) for v in header if cell_text(v)]
    next_filled = [cell_text(v) for v in nxt if cell_text(v)]
    if not next_filled:
        return False
    next_numeric = sum(1 for v in next_filled if _looks_numeric(v) or _looks_date(v))
    header_numeric = sum(1 for v in header_filled if _looks_numeric(v) or _looks_date(v))
    return next_numeric > header_numeric or len(next_filled) >= len(header_filled)


def choose_header_row_index(rows: Sequence[Sequence[Any] | None], *, max_scan: int = _HEADER_SCAN_ROWS) -> int:
    """Return the index of the most likely header row."""
    if not rows:
        return 0
    scan = min(len(rows), max_scan)
    best_i = 0
    best_score = -1.0
    for i in range(scan):
        row = rows[i]
        score = score_header_row(row)
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        if score >= 0 and _next_row_looks_like_data(row or [], nxt):
            score += 2.5
        if i + 1 >= len(rows):
            score -= 1
        if score > best_score:
            best_score = score
            best_i = i
    return best_i if best_score >= 0 else 0


def trim_trailing_empty(headers: list[Any]) -> list[Any]:
    trimmed = list(headers)
    while trimmed and not cell_text(trimmed[-1]):
        trimmed.pop()
    return trimmed


def split_header_and_data(rows: list[Sequence[Any]]) -> tuple[list[Any], list[list[Any]]]:
    """Choose a header row, drop title rows above it, return (headers, data_rows)."""
    if not rows:
        return [], []
    idx = choose_header_row_index(rows)
    headers = trim_trailing_empty(list(rows[idx] or []))
    data = [list(r) for r in rows[idx + 1 :]]
    return headers, data


def expression_references_schema_column(
    expression: str,
    *,
    column_names: set[str],
    column_refs: set[str],
) -> bool:
    """True when a SQL expression mentions a real column (not only a table name)."""
    expr = (expression or "").strip()
    if not expr:
        return False
    if _COUNT_STAR_RE.match(expr):
        return True
    lowered = expr.lower()
    for ref in column_refs:
        if ref and ref.lower() in lowered:
            return True
    idents = {m.group(0).lower() for m in _IDENT_RE.finditer(expr)} - _SQL_TOKENS
    cols = {c.lower() for c in column_names if c and not is_placeholder_column_name(c)}
    return bool(idents & cols)
