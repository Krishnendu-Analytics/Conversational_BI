"""Data dictionary: business descriptions of a connection's tables and columns.

The agent otherwise only sees table/column *names* and types, and has to
guess what ``fact_ar.outstanding_value`` means. Customers often already
have that knowledge in a spreadsheet (table_name, column_name,
description). This module imports it:

1. ``parse_dictionary_source`` reads the file by header (table / column /
   description, optional schema and table-description columns).
2. ``match_to_schema`` keeps only rows that name a real discovered table
   and column (case-insensitive), rewriting them to the canonical names;
   everything else is reported as skipped. Saving re-checks this server-
   side, so a client can never store descriptions for non-existent columns.
3. ``summarize_tables_with_llm`` optionally writes a one-line summary for
   each table that has column descriptions but no table description.
4. ``apply_to_schema`` merges stored descriptions into the schema dict the
   agent loads, so the SQL prompt can show them.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.client import get_llm
from app.llm.usage import ContextThreadPoolExecutor
from app.models import DataDictionaryEntry
from app.services.file_import import (
    ImportFileError,
    SourceDocument,
    clean_cell,
    norm_header,
)

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_LEN = 2000
MAX_SUMMARY_LEN = 400
_TABLES_PER_SUMMARY_CALL = 5
_WORKERS = 4
_MAX_OUTPUT_TOKENS = 4000

_SCHEMA_HEADERS = {"table schema", "schema", "schema name"}
_TABLE_HEADERS = {"table name", "table", "entity", "entity name", "object name"}
_COLUMN_HEADERS = {
    "column name", "column", "field name", "field", "attribute", "attribute name",
}
_DESCRIPTION_WORDS = ("description", "definition", "meaning", "explanation", "comment")

Key = tuple[str, str, str]  # (schema, table, column) lowercased; column "" = table-level


class DataDictionaryError(ImportFileError):
    """The file can't be used as a data dictionary."""


@dataclass
class DictRow:
    schema_name: str
    table_name: str
    column_name: str  # "" = table-level description
    description: str
    origin: str = "import"

    @property
    def key(self) -> Key:
        return (self.schema_name.lower(), self.table_name.lower(), self.column_name.lower())


@dataclass
class ParsedDictionary:
    rows: list[DictRow] = field(default_factory=list)
    skipped: list[tuple[int | None, str]] = field(default_factory=list)


# --- Parsing --------------------------------------------------------------------


def _find_header(rows: list[list[object]]) -> tuple[int, dict[str, int]] | None:
    for idx, row in enumerate(rows[:10]):
        headers = [norm_header(c) for c in row]
        cols: dict[str, int] = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            if h in _SCHEMA_HEADERS:
                cols.setdefault("schema", i)
            elif h in _TABLE_HEADERS:
                cols.setdefault("table", i)
            elif h in _COLUMN_HEADERS:
                cols.setdefault("column", i)
            elif any(w in h for w in _DESCRIPTION_WORDS):
                if "table" in h:
                    cols.setdefault("table_description", i)
                else:
                    cols.setdefault("description", i)
        has_description = "description" in cols or "table_description" in cols
        if has_description and ("table" in cols or "column" in cols):
            return idx, cols
    return None


def parse_dictionary_source(source: SourceDocument) -> ParsedDictionary:
    if not source.sheets:
        raise DataDictionaryError("No data found in this file")
    result = ParsedDictionary()
    seen: set[Key] = set()
    found_header = False

    def add(row_no: int, schema: str, table: str, column: str, description: str) -> None:
        if not description:
            return
        if len(description) > MAX_DESCRIPTION_LEN:
            result.skipped.append((row_no, f"description longer than {MAX_DESCRIPTION_LEN} characters"))
            return
        item = DictRow(schema, table, column, description)
        if item.key in seen:
            return  # repeated table description on every row, or a true duplicate
        seen.add(item.key)
        result.rows.append(item)

    for rows in source.sheets:
        header = _find_header(rows)
        if header is None:
            continue
        found_header = True
        start, cols = header

        def cell(row: list[object], name: str) -> str:
            i = cols.get(name)
            return clean_cell(row[i]) if i is not None and i < len(row) else ""

        for idx in range(start + 1, len(rows)):
            row = rows[idx]
            if not any(clean_cell(c) for c in row):
                continue
            schema, table, column = cell(row, "schema"), cell(row, "table"), cell(row, "column")
            if not table and "." in column:
                # "table.column" (or "schema.table.column") in one cell.
                parts = column.split(".")
                column, table = parts[-1], parts[-2]
                schema = parts[-3] if len(parts) > 2 else schema
            if not table:
                result.skipped.append((idx + 1, "no table name"))
                continue
            add(idx + 1, schema, table, "", cell(row, "table_description"))
            if column:
                add(idx + 1, schema, table, column, cell(row, "description"))
            elif "column" not in cols:
                add(idx + 1, schema, table, "", cell(row, "description"))

    if not found_header:
        raise DataDictionaryError(
            "Couldn't find the table, column and description columns. Use headers "
            'like "table_name", "column_name" and "description".'
        )
    if not result.rows:
        raise DataDictionaryError("No descriptions found in this file")
    return result


# --- Matching against the discovered schema ---------------------------------------


class _SchemaIndex:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.by_qualified: dict[tuple[str, str], dict] = {}
        self.by_table: dict[str, list[dict]] = {}
        for t in schema.get("tables") or []:
            if not isinstance(t, dict) or not t.get("table_name"):
                continue
            name = str(t["table_name"]).lower()
            self.by_qualified[(str(t.get("schema_name") or "").lower(), name)] = t
            self.by_table.setdefault(name, []).append(t)

    def resolve(self, row: DictRow) -> tuple[DictRow | None, str | None]:
        table_key = row.table_name.lower()
        if row.schema_name:
            table = self.by_qualified.get((row.schema_name.lower(), table_key))
        else:
            candidates = self.by_table.get(table_key) or []
            if len(candidates) > 1:
                return None, f'table "{row.table_name}" exists in several schemas; add a schema column'
            table = candidates[0] if candidates else None
        if table is None:
            return None, f'table "{row.table_name}" not found in this connection'
        column = ""
        if row.column_name:
            match = next(
                (
                    c for c in table.get("columns") or []
                    if isinstance(c, dict) and str(c.get("name") or "").lower() == row.column_name.lower()
                ),
                None,
            )
            if match is None:
                return None, f'column "{row.column_name}" not found in table "{table["table_name"]}"'
            column = str(match["name"])
        return (
            DictRow(
                str(table.get("schema_name") or ""),
                str(table["table_name"]),
                column,
                row.description,
                row.origin,
            ),
            None,
        )


def match_to_schema(rows: Iterable[DictRow], schema: dict[str, Any]) -> ParsedDictionary:
    """Keep rows naming a real table/column (canonical names); summarise the rest."""
    index = _SchemaIndex(schema)
    result = ParsedDictionary()
    misses: Counter[str] = Counter()
    seen: set[Key] = set()
    for row in rows:
        resolved, reason = index.resolve(row)
        if resolved is None:
            misses[reason or "not found"] += 1
            continue
        if resolved.key in seen:
            continue
        seen.add(resolved.key)
        result.rows.append(resolved)
    for reason, count in misses.most_common():
        result.skipped.append((None, reason if count == 1 else f"{reason} ({count} rows)"))
    return result


# --- AI table summaries -------------------------------------------------------------

_SUMMARY_PROMPT = """You are documenting a business database for a BI assistant.
For each table below, write ONE plain-English sentence saying what the table holds and, if evident, what one row represents.
Use ONLY the table name, column names and column descriptions given. Do not invent facts, sources or business rules.

Return ONLY JSON, no commentary:
{{"tables": [{{"table": "<exact table name as given>", "summary": "..."}}]}}

TABLES:
{tables}"""


def _table_block(table: dict, column_descriptions: dict[str, str]) -> str:
    name = str(table["table_name"])
    lines = [f"table {name}"]
    for col in table.get("columns") or []:
        if not isinstance(col, dict) or not col.get("name"):
            continue
        desc = column_descriptions.get(str(col["name"]).lower(), "")
        lines.append(f"- {col['name']} ({col.get('data_type') or '?'})" + (f": {desc}" if desc else ""))
    return "\n".join(lines)


def summarize_tables_with_llm(
    schema: dict[str, Any], rows: list[DictRow], existing: set[Key], llm=None
) -> tuple[list[DictRow], list[tuple[int | None, str]]]:
    """AI one-line summaries for tables that have column descriptions (in
    ``rows``) but no table-level description in ``rows`` or ``existing``."""
    col_desc: dict[tuple[str, str], dict[str, str]] = {}
    has_table_desc: set[tuple[str, str]] = {(k[0], k[1]) for k in existing if not k[2]}
    for r in rows:
        tkey = (r.schema_name.lower(), r.table_name.lower())
        if r.column_name:
            col_desc.setdefault(tkey, {})[r.column_name.lower()] = r.description
        else:
            has_table_desc.add(tkey)

    index = _SchemaIndex(schema)
    targets = [
        (tkey, index.by_qualified[tkey])
        for tkey in col_desc
        if tkey not in has_table_desc and tkey in index.by_qualified
    ]
    if not targets:
        return [], []

    batches = [
        targets[i : i + _TABLES_PER_SUMMARY_CALL]
        for i in range(0, len(targets), _TABLES_PER_SUMMARY_CALL)
    ]
    llm = llm or get_llm()

    def run(batch: list[tuple[tuple[str, str], dict]]) -> list[dict] | None:
        prompt = _SUMMARY_PROMPT.format(
            tables="\n\n".join(_table_block(t, col_desc[k]) for k, t in batch)
        )
        try:
            raw = llm.complete(prompt, max_output_tokens=_MAX_OUTPUT_TOKENS)
        except Exception:  # noqa: BLE001 — summaries are optional
            logger.exception("data dictionary table summary failed")
            return None
        text = (raw or "").strip()
        if not text or text.lower().startswith("[llm stub]"):
            return None
        match = re.search(r"\{[\s\S]*\}", text)
        try:
            data = json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            return None
        items = data.get("tables") if isinstance(data, dict) else None
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else None

    with ContextThreadPoolExecutor(max_workers=min(_WORKERS, len(batches))) as pool:
        responses = list(pool.map(run, batches))

    summaries: list[DictRow] = []
    skipped: list[tuple[int | None, str]] = []
    for batch, items in zip(batches, responses):
        if items is None:
            skipped.append((None, "AI could not summarise: " + ", ".join(t["table_name"] for _, t in batch)))
            continue
        by_name = {str(t["table_name"]).lower(): t for _, t in batch}
        for item in items:
            table = by_name.pop(str(item.get("table") or "").strip().lower(), None)
            summary = " ".join(str(item.get("summary") or "").split())
            if table is None or not summary:
                continue  # unknown/duplicate table name from the model: ignore
            summaries.append(
                DictRow(
                    str(table.get("schema_name") or ""),
                    str(table["table_name"]),
                    "",
                    summary[:MAX_SUMMARY_LEN],
                    origin="ai",
                )
            )
    return summaries, skipped


# --- Storage ----------------------------------------------------------------------


def load_entries(db: Session, tenant_id: UUID, connection_id: UUID) -> list[DataDictionaryEntry]:
    return list(
        db.scalars(
            select(DataDictionaryEntry)
            .where(
                DataDictionaryEntry.tenant_id == tenant_id,
                DataDictionaryEntry.connection_id == connection_id,
            )
            .order_by(
                DataDictionaryEntry.schema_name,
                DataDictionaryEntry.table_name,
                DataDictionaryEntry.column_name,
            )
        )
    )


def entry_key(entry: DataDictionaryEntry) -> Key:
    return (entry.schema_name.lower(), entry.table_name.lower(), entry.column_name.lower())


def upsert_entries(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    rows: list[DictRow],
    overwrite: bool,
) -> dict[str, int]:
    """Caller must have run ``match_to_schema`` on ``rows``. Does not commit."""
    existing = {entry_key(e): e for e in load_entries(db, tenant_id, connection_id)}
    counts = {"created": 0, "updated": 0, "unchanged": 0, "kept_existing": 0}
    for row in rows:
        current = existing.get(row.key)
        if current is None:
            entry = DataDictionaryEntry(
                tenant_id=tenant_id,
                connection_id=connection_id,
                schema_name=row.schema_name,
                table_name=row.table_name,
                column_name=row.column_name,
                description=row.description,
                origin=row.origin,
            )
            db.add(entry)
            existing[row.key] = entry
            counts["created"] += 1
        elif current.description == row.description:
            counts["unchanged"] += 1
        elif overwrite:
            current.description = row.description
            current.origin = row.origin
            counts["updated"] += 1
        else:
            counts["kept_existing"] += 1
    return counts


# --- Feeding the agent ---------------------------------------------------------------


def apply_to_schema(schema: dict[str, Any], entries: Iterable[DataDictionaryEntry]) -> dict[str, Any]:
    """Set ``description`` on matching tables/columns of a loaded schema dict."""
    by_table: dict[tuple[str, str], dict[str, str]] = {}
    for e in entries:
        by_table.setdefault((e.schema_name.lower(), e.table_name.lower()), {})[
            e.column_name.lower()
        ] = e.description
    if not by_table:
        return schema
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = str(table.get("table_name") or "").lower()
        descs = by_table.get((str(table.get("schema_name") or "").lower(), name)) or by_table.get(
            ("", name)
        )
        if not descs:
            continue
        if descs.get(""):
            table["description"] = descs[""]
        for col in table.get("columns") or []:
            if isinstance(col, dict):
                desc = descs.get(str(col.get("name") or "").lower())
                if desc:
                    col["description"] = desc
    return schema


def build_dictionary_preview(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    schema: dict[str, Any],
    filename: str,
    content: bytes,
    summarize: bool,
    llm=None,
) -> ParsedDictionary:
    """Parse, match to the connection's schema, and (optionally) add AI
    table summaries. Nothing is saved."""
    from app.services.file_import import load_source

    parsed = parse_dictionary_source(load_source(filename, content))
    matched = match_to_schema(parsed.rows, schema)
    if not matched.rows:
        reasons = "; ".join(r for _, r in matched.skipped[:3])
        raise DataDictionaryError(
            "None of the rows match a table in this connection"
            + (f" ({reasons})" if reasons else "")
        )
    result = ParsedDictionary(rows=matched.rows, skipped=parsed.skipped + matched.skipped)
    if summarize:
        existing = {entry_key(e) for e in load_entries(db, tenant_id, connection_id)}
        summaries, failed = summarize_tables_with_llm(schema, matched.rows, existing, llm)
        result.rows = summaries + result.rows
        result.skipped += failed
    return result
