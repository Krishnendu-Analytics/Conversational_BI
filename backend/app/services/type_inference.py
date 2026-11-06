"""Deterministic physical type inference for tabular file columns."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


PHYSICAL_TYPES = ("string", "integer", "decimal", "boolean", "date", "datetime", "null")


def normalize_column_name(display: str, *, used: set[str]) -> tuple[str, str]:
    """Return (internal_name, display_name). Internal names are snake_case and unique."""
    display = (display or "").strip() or "column"
    internal = re.sub(r"[^a-zA-Z0-9]+", "_", display).strip("_").lower()
    if not internal:
        internal = "column"
    if internal[0].isdigit():
        internal = f"c_{internal}"
    base = internal[:63]
    candidate = base
    n = 2
    while candidate in used:
        candidate = f"{base}_{n}"
        n += 1
    used.add(candidate)
    return candidate, display


def _try_int(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _try_decimal(value: str) -> bool:
    try:
        Decimal(value.replace(",", ""))
        return True
    except (InvalidOperation, ValueError):
        return False


def _try_bool(value: str) -> bool:
    return value.lower() in {"true", "false", "yes", "no", "0", "1", "y", "n"}


def _try_date(value: str) -> bool:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            datetime.strptime(value[:10], fmt)
            return True
        except ValueError:
            continue
    return False


def infer_column_type(values: list[Any], *, sample_size: int = 500) -> str:
    """Infer a single physical type from non-null sample values."""
    samples: list[str] = []
    for raw in values:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        samples.append(str(raw).strip())
        if len(samples) >= sample_size:
            break
    if not samples:
        return "null"

    if all(_try_bool(v) for v in samples):
        return "boolean"
    if all(_try_int(v) for v in samples):
        return "integer"
    if all(_try_decimal(v) for v in samples):
        return "decimal"
    if all(_try_date(v) for v in samples):
        return "date"
    return "string"


def duckdb_type_for(physical: str) -> str:
    mapping = {
        "string": "VARCHAR",
        "integer": "BIGINT",
        "decimal": "DOUBLE",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "datetime": "TIMESTAMP",
        "null": "VARCHAR",
    }
    return mapping.get(physical, "VARCHAR")
