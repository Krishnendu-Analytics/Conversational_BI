"""Read an uploaded CSV / Excel file into rows, for semantic-layer imports.

Shared by the data dictionary importer. Only tabular formats are accepted:
every import here needs named columns (table, column, description, ...),
which free-text formats such as PDF cannot provide reliably.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

MAX_FILE_BYTES = 5 * 1024 * 1024


class ImportFileError(ValueError):
    """The file cannot be read at all (wrong type, empty, too large, corrupt)."""


@dataclass
class SourceDocument:
    """A loaded upload: one list of rows per sheet (a CSV is one sheet)."""

    sheets: list[list[list[object]]] = field(default_factory=list)
    # Sheet titles, aligned with ``sheets`` when known (empty for CSV).
    names: list[str] = field(default_factory=list)

    def name(self, index: int) -> str:
        return self.names[index] if index < len(self.names) else f"Sheet {index + 1}"


def clean_cell(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def norm_header(value: object) -> str:
    """ "Column_Name" / "column-name" / " Column  Name " -> "column name"."""
    return re.sub(r"[_\-]+", " ", clean_cell(value)).lower()


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _read_csv(content: bytes) -> list[list[object]]:
    text = _decode(content)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def _read_xlsx(content: bytes) -> SourceDocument:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl raises many types for bad files
        raise ImportFileError("Could not read the Excel file; save it as .xlsx and try again") from exc
    try:
        sheets = [
            (str(ws.title), [list(r) for r in ws.iter_rows(values_only=True)])
            for ws in wb.worksheets
        ]
    finally:
        wb.close()
    kept = [(n, rows) for n, rows in sheets if any(clean_cell(c) for row in rows for c in row)]
    return SourceDocument(sheets=[rows for _, rows in kept], names=[n for n, _ in kept])


def load_source(filename: str, content: bytes) -> SourceDocument:
    if not content:
        raise ImportFileError("The file is empty")
    if len(content) > MAX_FILE_BYTES:
        raise ImportFileError("File is larger than 5 MB")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("csv", "txt", "tsv"):
        return SourceDocument(sheets=[_read_csv(content)])
    if ext in ("xlsx", "xlsm"):
        return _read_xlsx(content)
    if ext == "xls":
        raise ImportFileError("Old .xls files aren't supported; save it as .xlsx and try again")
    raise ImportFileError("Unsupported file type; upload a .csv or .xlsx file")
