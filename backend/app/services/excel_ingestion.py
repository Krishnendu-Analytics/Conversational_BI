"""Excel workbook parsing and loading."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.dataset_errors import IngestionLimitError, MalformedFileError, UnsupportedFileTypeError
from app.services.tabular_headers import cell_text, split_header_and_data
from app.services.type_inference import duckdb_type_for, infer_column_type, normalize_column_name


def _load_workbook(content: bytes):
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise UnsupportedFileTypeError("Excel support is not installed") from exc

    import io

    try:
        return load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise MalformedFileError("Could not read Excel workbook") from exc


def list_excel_sheets(content: bytes) -> list[str]:
    wb = _load_workbook(content)
    return [str(name) for name in wb.sheetnames]


def parse_excel_sheet(content: bytes, sheet_name: str) -> dict[str, Any]:
    settings = get_settings()
    wb = _load_workbook(content)
    if sheet_name not in wb.sheetnames:
        raise MalformedFileError(f"Worksheet '{sheet_name}' not found")

    ws = wb[sheet_name]
    raw_rows: list[list[Any]] = []
    for row in ws.iter_rows(values_only=True):
        if row is None:
            continue
        raw_rows.append(list(row))
        if len(raw_rows) > settings.file_max_rows + 20:
            raise IngestionLimitError(f"Sheet exceeds maximum row count ({settings.file_max_rows})")

    header_row, data_rows = split_header_and_data(raw_rows)
    headers = [cell_text(h) for h in header_row]
    if not any(headers):
        raise MalformedFileError("Worksheet header row is empty")
    if len(headers) > settings.file_max_columns:
        raise IngestionLimitError(f"Sheet exceeds maximum column count ({settings.file_max_columns})")

    data_rows = [
        row
        for row in data_rows
        if not all(c is None or cell_text(c) == "" for c in row)
    ]
    if len(data_rows) > settings.file_max_rows:
        raise IngestionLimitError(f"Sheet exceeds maximum row count ({settings.file_max_rows})")

    used: set[str] = set()
    columns: list[dict[str, Any]] = []
    col_indices: list[int] = []
    for idx, header in enumerate(headers):
        if not header:
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
        raise MalformedFileError("Worksheet has no named columns")

    normalized_rows: list[list[Any]] = []
    for row in data_rows:
        normalized_rows.append([row[i] if i < len(row) else None for i in col_indices])

    return {
        "sheet_name": sheet_name,
        "columns": columns,
        "rows": normalized_rows,
        "row_count": len(normalized_rows),
    }


def discover_excel_sheets(content: bytes) -> list[dict[str, Any]]:
    """Return metadata for non-empty worksheets."""
    wb = _load_workbook(content)
    sheets: list[dict[str, Any]] = []
    for name in wb.sheetnames:
        try:
            spec = parse_excel_sheet(content, name)
            if spec["row_count"] > 0:
                sheets.append({"sheet_name": name, "row_count": spec["row_count"]})
        except MalformedFileError:
            continue
    return sheets
