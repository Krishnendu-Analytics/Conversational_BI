"""Example business questions for a connection, and evaluating the
assistant against them.

A semantic-model workbook often lists sample questions with the tables a
correct answer needs ("Net sales by product this year" -> fact_sales,
dim_product, dim_date). Stored per connection, they become a small test
set: ``evaluate_examples`` asks the assistant each question, runs the SQL
it writes (read-only, row-limited) and checks it used the expected tables.
Only pass/fail, tables used and errors are reported, never the SQL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.usage import ContextThreadPoolExecutor
from app.models import SemanticExample
from app.services.file_import import clean_cell, norm_header

MAX_QUESTION_LEN = 500
MAX_EVALUATED = 20
_WORKERS = 4

_QUESTION_HEADERS = {
    "example question", "question", "sample question", "user question",
    "business question", "nl question", "natural language question", "example",
}
_TABLES_HEADERS = {
    "semantic sources", "tables", "expected tables", "source tables", "sources",
    "data sources", "tables used",
}
_DOMAIN_HEADERS = {"domain", "category", "area", "business domain", "subject area"}
_NOTES_HEADERS = {"key concepts", "concepts", "notes", "expected answer", "answer", "hint", "metric"}


@dataclass
class ExampleRow:
    question: str
    tables: list[str] = field(default_factory=list)  # as written in the file
    domain: str = ""
    notes: str = ""
    origin: str = "import"
    row: int | None = None

    @property
    def key(self) -> str:
        return question_key(self.question)


def question_key(question: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", question.lower()))[:300]


def _examples_header(rows: list[list[object]]) -> tuple[int, dict[str, int]] | None:
    for idx, row in enumerate(rows[:10]):
        headers = [norm_header(c) for c in row]
        question = next((i for i, h in enumerate(headers) if h in _QUESTION_HEADERS), None)
        if question is None:
            continue
        cols = {"question": question}
        for key, names in (("tables", _TABLES_HEADERS), ("domain", _DOMAIN_HEADERS), ("notes", _NOTES_HEADERS)):
            found = next(
                (i for i, h in enumerate(headers) if h in names and i not in cols.values()), None
            )
            if found is not None:
                cols[key] = found
        return idx, cols
    return None


def split_tables(text: str) -> list[str]:
    parts = re.split(r"\s*(?:[+,;/&|]|\band\b)\s*", text or "")
    return [p.strip() for p in parts if p.strip()]


def parse_example_sheets(
    sheets: list[list[list[object]]],
) -> tuple[list[ExampleRow], list[tuple[int | None, str]], set[int]]:
    rows_out: list[ExampleRow] = []
    skipped: list[tuple[int | None, str]] = []
    used: set[int] = set()
    seen: set[str] = set()
    for sheet_index, rows in enumerate(sheets):
        header = _examples_header(rows)
        if header is None:
            continue
        used.add(sheet_index)
        start, cols = header

        def cell(row: list[object], name: str) -> str:
            i = cols.get(name)
            return clean_cell(row[i]) if i is not None and i < len(row) else ""

        for idx in range(start + 1, len(rows)):
            row = rows[idx]
            question = cell(row, "question")
            if not question:
                continue
            if len(question) > MAX_QUESTION_LEN:
                skipped.append((idx + 1, "question is too long"))
                continue
            item = ExampleRow(
                question=question,
                tables=split_tables(cell(row, "tables")),
                domain=cell(row, "domain")[:120],
                notes=cell(row, "notes")[:2000],
                row=idx + 1,
            )
            if not item.key or item.key in seen:
                continue
            seen.add(item.key)
            rows_out.append(item)
    return rows_out, skipped, used


# --- Schema grounding ------------------------------------------------------------------


def _table_index(schema: dict[str, Any]) -> dict[str, str]:
    """lowercased bare and qualified name -> qualified name."""
    out: dict[str, str] = {}
    for t in schema.get("tables") or []:
        if not isinstance(t, dict) or not t.get("table_name"):
            continue
        name = str(t["table_name"])
        qualified = f"{t['schema_name']}.{name}" if t.get("schema_name") else name
        out.setdefault(name.lower(), qualified)
        out[qualified.lower()] = qualified
    return out


def resolve_tables(names: list[str], schema: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(known qualified tables, names that aren't tables). Generic words
    such as "dimensions" are simply not tables."""
    index = _table_index(schema)
    known: list[str] = []
    unknown: list[str] = []
    for name in names:
        hit = index.get(name.lower())
        if hit:
            if hit not in known:
                known.append(hit)
        elif re.fullmatch(r"[A-Za-z_][\w.]*", name) and "_" in name:
            unknown.append(name)
    return known, unknown


def example_warnings(item: ExampleRow, schema: dict[str, Any]) -> list[str]:
    _known, unknown = resolve_tables(item.tables, schema)
    if unknown:
        return [f"{', '.join(unknown[:4])} not found in this connection"]
    return []


# --- Storage --------------------------------------------------------------------------------


def load_examples(db: Session, tenant_id: UUID, connection_id: UUID) -> list[SemanticExample]:
    return list(
        db.scalars(
            select(SemanticExample)
            .where(
                SemanticExample.tenant_id == tenant_id,
                SemanticExample.connection_id == connection_id,
            )
            .order_by(SemanticExample.domain, SemanticExample.created_at)
        )
    )


def example_tables(row: SemanticExample) -> list[str]:
    try:
        value = json.loads(row.tables_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def existing_keys(db: Session, tenant_id: UUID, connection_id: UUID) -> set[str]:
    return {r.question_key for r in load_examples(db, tenant_id, connection_id)}


def save_examples(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    rows: list[ExampleRow],
    schema: dict[str, Any],
) -> dict[str, int]:
    """Store new questions (tables resolved to real ones). Does not commit."""
    counts = {"created": 0, "unchanged": 0, "rejected": 0}
    known_keys = existing_keys(db, tenant_id, connection_id)
    for item in rows:
        if not item.key or len(item.question) > MAX_QUESTION_LEN:
            counts["rejected"] += 1
            continue
        if item.key in known_keys:
            counts["unchanged"] += 1
            continue
        tables, _unknown = resolve_tables(item.tables, schema)
        db.add(
            SemanticExample(
                tenant_id=tenant_id,
                connection_id=connection_id,
                question=item.question,
                question_key=item.key,
                tables_json=json.dumps(tables) if tables else None,
                domain=item.domain,
                notes=item.notes,
                origin=item.origin if item.origin in ("import", "ai") else "import",
            )
        )
        known_keys.add(item.key)
        counts["created"] += 1
    return counts


# --- Evaluation ------------------------------------------------------------------------------


@dataclass
class Answer:
    sql: str | None
    error: str | None = None
    fallback: bool = False  # the assistant fell back to a heuristic guess


@dataclass
class ExampleResult:
    question: str
    status: str  # pass | wrong_tables | fallback | error | no_answer
    expected_tables: list[str] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    row_count: int | None = None
    error: str | None = None


def tables_in_sql(sql: str, schema: dict[str, Any]) -> list[str]:
    lowered = sql.lower()
    used: list[str] = []
    for t in schema.get("tables") or []:
        if not isinstance(t, dict) or not t.get("table_name"):
            continue
        name = str(t["table_name"])
        if re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", lowered):
            qualified = f"{t['schema_name']}.{name}" if t.get("schema_name") else name
            if qualified not in used:
                used.append(qualified)
    return used


def evaluate_one(
    question: str,
    expected: list[str],
    schema: dict[str, Any],
    answer: Callable[[str], Answer],
    execute: Callable[[str], list[dict]],
) -> ExampleResult:
    result = ExampleResult(question=question, status="error", expected_tables=expected)
    try:
        reply = answer(question)
    except Exception as exc:  # noqa: BLE001
        result.error = _short(exc)
        return result
    if not reply.sql:
        result.status = "no_answer"
        result.error = reply.error or "the assistant did not write a query"
        return result
    result.tables_used = tables_in_sql(reply.sql, schema)
    try:
        rows = execute(reply.sql)
    except Exception as exc:  # noqa: BLE001
        result.error = _short(exc)
        return result
    result.row_count = len(rows)
    used = {t.lower() for t in result.tables_used}
    result.missing_tables = [t for t in expected if t.lower() not in used]
    if reply.fallback:
        result.status = "fallback"
    elif result.missing_tables:
        result.status = "wrong_tables"
    else:
        result.status = "pass"
    return result


def evaluate_examples(
    examples: list[SemanticExample],
    schema: dict[str, Any],
    *,
    answer: Callable[[str], Answer],
    execute: Callable[[str], list[dict]],
) -> list[ExampleResult]:
    items = examples[:MAX_EVALUATED]
    if not items:
        return []
    with ContextThreadPoolExecutor(max_workers=min(_WORKERS, len(items))) as pool:
        return list(
            pool.map(
                lambda e: evaluate_one(e.question, example_tables(e), schema, answer, execute),
                items,
            )
        )


def _short(exc: Exception) -> str:
    return (" ".join(str(exc).split())[:300]) or exc.__class__.__name__
