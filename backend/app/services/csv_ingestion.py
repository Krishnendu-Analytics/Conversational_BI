"""CSV parsing and loading into DuckDB."""

from __future__ import annotations

import csv
import io
from typing import Any

from app.dataset_errors import IngestionLimitError, MalformedFileError
from app.config import get_settings
from app.services.tabular_headers import cell_text, split_header_and_data
from app.services.type_inference import duckdb_type_for, infer_column_type, normalize_column_name


def _detect_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


def parse_csv_bytes(content: bytes) -> tuple[list[str], list[list[Any]]]:
    """Parse CSV bytes into display headers and row values."""
    settings = get_settings()
    text = content.decode("utf-8-sig")
    if not text.strip():
        raise MalformedFileError("CSV file is empty")

    sample = text[:4096]
    delimiter = _detect_delimiter(sample)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        raise MalformedFileError("CSV has no rows")

    header_row, data_rows = split_header_and_data(rows)
    headers = [cell_text(h) for h in header_row]
    if not any(headers):
        raise MalformedFileError("CSV header row is empty")
    if len(headers) > settings.file_max_columns:
        raise IngestionLimitError(f"CSV exceeds maximum column count ({settings.file_max_columns})")

    if len(data_rows) > settings.file_max_rows:
        raise IngestionLimitError(f"CSV exceeds maximum row count ({settings.file_max_rows})")

    return headers, data_rows


def build_csv_table_spec(content: bytes) -> dict[str, Any]:
    """Return column specs and rows for DuckDB load."""
    display_headers, data_rows = parse_csv_bytes(content)
    used: set[str] = set()
    columns: list[dict[str, Any]] = []
    col_indices: list[int] = []

    for idx, header in enumerate(display_headers):
        if not str(header or "").strip():
            continue
        internal, display = normalize_column_name(header, used=used)
        col_values = [row[idx] if idx < len(row) else None for row in data_rows]
        physical = infer_column_type(col_values)
        columns.append(
            {
                "internal_name": internal,
                "display_name": display,
                "physical_type": physical,
                "duckdb_type": duckdb_type_for(physical),
                "nullable": True,
            }
        )
        col_indices.append(idx)

    if not columns:
        raise MalformedFileError("CSV has no named columns")

    normalized_rows: list[list[Any]] = []
    for row in data_rows:
        normalized_rows.append([row[i] if i < len(row) else None for i in col_indices])

    return {
        "columns": columns,
        "rows": normalized_rows,
        "row_count": len(normalized_rows),
    }
