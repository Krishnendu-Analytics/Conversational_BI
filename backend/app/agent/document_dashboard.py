"""Build a dashboard from attached documents (PDF, image, office files).

The database dashboard builder only reads the connected database, so "make a
dashboard of the attached PDF" used to return an unrelated sales board. Here
the LLM reads the document text and proposes KPIs, charts and tables; every
figure it returns is then checked against the numbers actually written in the
document, and anything not found there is dropped. Widgets carry their rows
inline (no SQL), so refreshing the board never re-queries the database.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any
from uuid import UUID, uuid4

from app.agent.prompts import get_prompt

logger = logging.getLogger(__name__)

DOCUMENT_SOURCE = "document"
BUILD_MODE = "document"
# Enough for a multi-page invoice or statement; keeps the prompt bounded.
MAX_DOCUMENT_CHARS = 24_000
_MAX_KPIS = 6
_MAX_CHARTS = 4
_MAX_TABLES = 3
_MAX_POINTS = 30
_MAX_TABLE_ROWS = 200
_CHART_KINDS = {"bar", "pie", "line"}

_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
def is_document_widget(widget: dict[str, Any]) -> bool:
    config = widget.get("config") if isinstance(widget.get("config"), dict) else {}
    return widget.get("source") == DOCUMENT_SOURCE or config.get("source") == DOCUMENT_SOURCE


def wants_document_dashboard(*, attached_ids: list[str] | None) -> bool:
    """Should a dashboard request be built from documents, not the database?

    Only when files are attached to the request, or the user or supervisor
    pointed it at files (@file, or the supervisor's build_dashboard with
    files) — both arrive as ``attached_ids``. Wording ("the pdf", "this
    invoice") used to decide this, in English only; the supervisor now
    decides by meaning which files, if any, a dashboard should use."""
    return bool(attached_ids)


def load_document_text(
    db: Any, tenant_id: str, document_ids: list[str], max_chars: int = MAX_DOCUMENT_CHARS
) -> tuple[str, list[dict[str, str]]]:
    """Full text of the documents in reading order, plus [{id, filename}]."""
    from app.models import Document, DocumentChunk

    ids = [UUID(str(d)) for d in document_ids]
    docs = db.query(Document.id, Document.filename, Document.original_filename).filter(
        Document.tenant_id == UUID(str(tenant_id)), Document.id.in_(ids)
    )
    names = {str(d.id): str(d.original_filename or d.filename or "document") for d in docs}
    parts: list[str] = []
    used = 0
    sources: list[dict[str, str]] = []
    for doc_id in [str(i) for i in ids]:
        if doc_id not in names:
            continue
        chunks = (
            db.query(DocumentChunk.content)
            .filter(
                DocumentChunk.tenant_id == UUID(str(tenant_id)),
                DocumentChunk.document_id == UUID(doc_id),
            )
            .order_by(DocumentChunk.chunk_index)
        )
        body = "\n".join(str(c.content or "") for c in chunks).strip()
        if not body:
            continue
        header = f"=== Document: {names[doc_id]} ===\n"
        room = max_chars - used - len(header)
        if room <= 200:
            break
        parts.append(header + body[:room])
        used += len(header) + min(len(body), room)
        sources.append({"id": doc_id, "filename": names[doc_id]})
    return "\n\n".join(parts), sources


def _document_numbers(text: str) -> list[float]:
    out: list[float] = []
    for token in _NUMBER.findall(text):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        cleaned = re.sub(r"[^\d.\-]", "", value)
        if not cleaned or cleaned in {"-", ".", "-."}:
            return None
        try:
            num = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    return num if math.isfinite(num) else None


def _grounded(value: float, doc_numbers: list[float]) -> bool:
    """The figure (or its absolute value) is written in the document."""
    target = abs(value)
    return any(abs(abs(n) - target) <= max(0.005, target * 1e-9) for n in doc_numbers)


def _table_totals(tables: list[dict[str, Any]]) -> tuple[dict[str, list[float]], list[float]]:
    """Sums the document's own line items support: per label (every numeric
    column summed over rows whose text cell equals the label) and per column
    (whole-table totals). Lets "AAR: 26,630" (five AAR lines) count as
    verified even though that total is never written in the document."""
    by_label: dict[str, list[float]] = {}
    columns_total: list[float] = []
    for table in tables:
        rows = table.get("rows") or []
        if not rows:
            continue
        numeric_cols = [c for c in rows[0] if any(isinstance(r.get(c), (int, float)) for r in rows)]
        for col in numeric_cols:
            columns_total.append(sum(float(r.get(col) or 0) for r in rows if isinstance(r.get(col), (int, float))))
        groups: dict[str, dict[str, float]] = {}
        for row in rows:
            for cell in row.values():
                if isinstance(cell, str) and cell.strip():
                    bucket = groups.setdefault(cell.strip().lower(), {})
                    for col in numeric_cols:
                        if isinstance(row.get(col), (int, float)):
                            bucket[col] = bucket.get(col, 0.0) + float(row[col])
        for label, sums in groups.items():
            by_label.setdefault(label, []).extend(sums.values())
    return by_label, columns_total


def _base_label(label: str) -> str:
    """"AAR (7*)" -> "aar": charts often decorate the document's item name."""
    return re.sub(r"\s*\(.*?\)\s*$", "", label).strip().lower()


def _clean_number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else round(value, 4)


def _parse_json(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _widget(wtype: str, title: str, **fields: Any) -> dict[str, Any]:
    config = {"source": DOCUMENT_SOURCE}
    if fields.get("chart"):
        config["chart"] = fields["chart"]
    return {
        "id": str(uuid4()),
        "type": wtype,
        "title": title[:120],
        "sql": None,
        "source": DOCUMENT_SOURCE,
        "config": config,
        **fields,
    }


def _build_tables(plan: dict[str, Any], doc_numbers: list[float]) -> tuple[list[dict[str, Any]], int]:
    widgets: list[dict[str, Any]] = []
    dropped = 0
    for table in (plan.get("tables") or [])[:_MAX_TABLES]:
        if not isinstance(table, dict):
            continue
        title = str(table.get("title") or "").strip()
        columns = [str(c).strip() or f"Column {i + 1}" for i, c in enumerate(table.get("columns") or [])]
        if not title or not columns:
            continue
        rows = []
        for raw_row in (table.get("rows") or [])[:_MAX_TABLE_ROWS]:
            if not isinstance(raw_row, list):
                continue
            cells = list(raw_row[: len(columns)]) + [None] * (len(columns) - len(raw_row))
            ungrounded = False
            row: dict[str, Any] = {}
            for col, cell in zip(columns, cells):
                num = _to_number(cell) if isinstance(cell, (int, float)) else None
                if num is not None:
                    if not _grounded(num, doc_numbers):
                        ungrounded = True
                        break
                    row[col] = _clean_number(num)
                else:
                    row[col] = cell
            if ungrounded:
                dropped += 1
                continue
            rows.append(row)
        rows = _drop_extraction_duplicates(rows, doc_numbers)
        if rows:
            widgets.append(_widget("table", title, chart="table", rows=rows))
    return widgets, dropped


def _drop_extraction_duplicates(
    rows: list[dict[str, Any]], doc_numbers: list[float]
) -> list[dict[str, Any]]:
    """PDF text extraction sometimes repeats a line (page overlap). Identical
    rows are removed only when that makes a column add up to a total written
    in the document and keeping them does not: the bill reconciles itself."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = json.dumps(row, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    if len(unique) == len(rows):
        return rows
    for col in rows[0]:
        if not all(isinstance(r.get(col), (int, float)) for r in rows):
            continue
        with_dups = sum(float(r[col]) for r in rows)
        without = sum(float(r[col]) for r in unique)
        if _grounded(without, doc_numbers) and not _grounded(with_dups, doc_numbers):
            return unique
    return rows


# Rates, prices, shares and averages are per-unit figures: adding two lines'
# rates together means nothing, so they are never summed per item.
_NON_ADDITIVE = re.compile(r"rate|price|%|percent|avg|average|\bper\b|ratio|margin", re.IGNORECASE)


_SERIAL_COLUMN = re.compile(r"^\s*(no|sl|sr|s\.?\s*no|serial|id|#|code|lot)\b\.?", re.IGNORECASE)
_AMOUNT_WORDS = {"amount", "total", "value", "expenditure", "sales", "cost", "net", "gross"}


def _words(name: str) -> set[str]:
    return set(re.findall(r"[a-z]+", name.lower()))


def _resolve_column(rows: list[dict[str, Any]], wanted: str, *, numeric: bool) -> str:
    """The table column the LLM meant. Models often reuse the prompt
    example's names ("Item", "Amount") for a table whose columns are
    "Description" / "Expenditure Rs.", so fall back by meaning: the first
    text column for labels, the best-named (else last) additive numeric
    column for values."""
    if not rows:
        return ""
    columns = list(rows[0].keys())
    exact = {c.lower(): c for c in columns}
    if wanted.lower() in exact:
        return exact[wanted.lower()]
    is_num = {
        c: sum(isinstance(r.get(c), (int, float)) for r in rows) > len(rows) / 2 for c in columns
    }
    candidates = [c for c in columns if is_num[c] == numeric]
    if numeric:
        candidates = [c for c in candidates if not _NON_ADDITIVE.search(c)]
    if not candidates:
        return ""
    want = _words(wanted) | (_AMOUNT_WORDS if numeric and _words(wanted) & _AMOUNT_WORDS else set())
    scored = [(len(_words(c) & want), i, c) for i, c in enumerate(candidates)]
    best = max(scored)
    if best[0] > 0:
        return best[2]
    if numeric:
        # Bills put the amount last, after quantity and rate.
        return candidates[-1]
    # Labels: the text column that reads most like names, not a serial or
    # lot column ("7*", "No.", "ID").
    def wordiness(col: str) -> float:
        if _SERIAL_COLUMN.search(col):
            return -1.0
        cells = [str(r.get(col) or "") for r in rows]
        return sum(len(re.findall(r"[A-Za-z]", c)) for c in cells) / max(1, len(cells))

    return max(candidates, key=lambda c: (wordiness(c), -candidates.index(c)))


def _chart_rows_from_table(
    chart: dict[str, Any], tables_by_title: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]] | None, str, str]:
    """Chart computed from an already-verified table the LLM points at:
    the model picks the columns, the numbers come from the table."""
    table = tables_by_title.get(str(chart.get("table") or "").strip().lower())
    label_col = str(chart.get("label_column") or "").strip()
    value_col = str(chart.get("value_column") or "").strip()
    if table is None or not label_col or not value_col:
        return None, label_col, value_col
    rows_in = table.get("rows") or []
    label_col = _resolve_column(rows_in, label_col, numeric=False)
    value_col = _resolve_column(rows_in, value_col, numeric=True)
    if not label_col or not value_col or label_col == value_col:
        return None, label_col, value_col
    rows = [
        {label_col: str(r.get(label_col)).strip(), value_col: r[value_col]}
        for r in rows_in
        if isinstance(r.get(value_col), (int, float)) and str(r.get(label_col) or "").strip()
    ]
    return rows, label_col, value_col


def _merge_repeated_labels(
    rows: list[dict[str, Any]], label_name: str, value_name: str
) -> list[dict[str, Any]] | None:
    """Lot-wise documents repeat an item (five "AAR" lines): one bar per item,
    summed, or the biggest item looks like several small ones. Every part is
    verified, so the sum is too. None when the repeats can't be summed."""
    merged: dict[str, dict[str, Any]] = {}
    repeated = False
    for row in rows:
        key = _base_label(row[label_name]) or row[label_name].lower()
        if key in merged:
            repeated = True
            merged[key][value_name] = _clean_number(merged[key][value_name] + row[value_name])
            merged[key][label_name] = re.sub(r"\s*\(.*?\)\s*$", "", merged[key][label_name])
        else:
            merged[key] = dict(row)
    if repeated and _NON_ADDITIVE.search(value_name):
        return None
    return sorted(merged.values(), key=lambda r: r[value_name], reverse=True)


def build_widgets(plan: dict[str, Any], document_text: str) -> tuple[list[dict[str, Any]], int]:
    """Validated widgets from the LLM plan, and how many figures were dropped
    because neither the document nor a sum of its line items shows them."""
    doc_numbers = _document_numbers(document_text)
    tables, dropped = _build_tables(plan, doc_numbers)
    by_label, column_totals = _table_totals(tables)
    widgets: list[dict[str, Any]] = []

    for kpi in plan.get("kpis") or []:
        if not isinstance(kpi, dict) or len(widgets) >= _MAX_KPIS:
            continue
        value = _to_number(kpi.get("value"))
        label = str(kpi.get("label") or "").strip()
        if value is None or not label:
            continue
        if not (_grounded(value, doc_numbers) or _grounded(value, column_totals)):
            dropped += 1
            continue
        unit = str(kpi.get("unit") or "").strip() or None
        widgets.append(
            _widget("kpi", label, value=_clean_number(value), unit=unit,
                    rows=[{"value": _clean_number(value)}], value_kind="amount")
        )

    tables_by_title = {str(t["title"]).strip().lower(): t for t in tables}
    for chart in (plan.get("charts") or [])[:_MAX_CHARTS]:
        if not isinstance(chart, dict):
            continue
        title = str(chart.get("title") or "").strip()
        kind = str(chart.get("chart") or "bar").lower()
        kind = kind if kind in _CHART_KINDS else "bar"
        if chart.get("table"):
            rows, label_name, value_name = _chart_rows_from_table(chart, tables_by_title)
            if rows is None:
                continue
        else:
            label_name = str(chart.get("label_name") or "Label").strip() or "Label"
            value_name = str(chart.get("value_name") or "Value").strip() or "Value"
            if value_name == label_name:
                value_name = f"{value_name} value"
            rows = []
            bad_points = 0
            for point in (chart.get("points") or [])[:_MAX_POINTS]:
                if not isinstance(point, dict):
                    continue
                value = _to_number(point.get("value"))
                label = str(point.get("label") or "").strip()
                if value is None or not label:
                    continue
                derived = by_label.get(label.lower(), []) + by_label.get(_base_label(label), [])
                if not (_grounded(value, doc_numbers) or _grounded(value, derived)):
                    bad_points += 1
                    continue
                rows.append({label_name: label, value_name: _clean_number(value)})
            if bad_points:
                # A breakdown missing some of its parts misleads (the biggest
                # slice may be the one that is gone): drop the whole chart.
                dropped += bad_points
                continue
        if kind != "line":
            # A blank line on a bill ("Samity: -") is not a zero-sized part.
            rows = [r for r in rows if r[value_name] != 0]
            rows = _merge_repeated_labels(rows, label_name, value_name)
            if rows is None:
                continue
        if kind == "pie" and any(r[value_name] < 0 for r in rows):
            kind = "bar"
        if title and len(rows) >= 2:
            widgets.append(
                _widget("chart", title, chart=kind, rows=rows[:_MAX_POINTS], metric=value_name,
                        dimension=label_name, purpose="trend" if kind == "line" else "breakdown")
            )

    return widgets + tables, dropped


def grounded_summary(summary: str, document_text: str) -> str:
    """Drop summary sentences quoting a number the document does not show."""
    doc_numbers = _document_numbers(document_text)
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", summary.strip()):
        values = [_to_number(t) for t in _NUMBER.findall(sentence)]
        if all(v is None or _grounded(v, doc_numbers) for v in values):
            kept.append(sentence)
    return " ".join(kept).strip()


def build_document_dashboard(
    *, question: str, document_text: str, sources: list[dict[str, str]], llm: Any
) -> dict[str, Any]:
    """Dashboard spec (widgets hold their rows) or one with no data widgets
    and a ``fallback_reason`` explaining why."""
    names = ", ".join(s["filename"] for s in sources) or "the attached document"
    base: dict[str, Any] = {
        "title": f"Dashboard - {names}"[:120],
        "widgets": [],
        "filters": [],
        "build_mode": BUILD_MODE,
        "layout": {"source_documents": sources, "build_mode": BUILD_MODE},
        "source_documents": sources,
    }
    if not document_text.strip():
        base["fallback_reason"] = f"I could not read any text from {names}."
        return base

    prompt = get_prompt("document_dashboard").render(
        question=question, document_text=document_text
    )
    try:
        plan = _parse_json(llm.complete(prompt, max_output_tokens=4000))
    except Exception:  # noqa: BLE001
        logger.exception("document_dashboard_llm_failed")
        plan = None
    if plan is None:
        base["fallback_reason"] = (
            f"I read {names} but could not turn it into a dashboard right now. Please try again."
        )
        return base

    widgets, dropped = build_widgets(plan, document_text)
    if plan.get("title"):
        base["title"] = str(plan["title"]).strip()[:120]
    summary = grounded_summary(str(plan.get("summary") or ""), document_text)
    if summary:
        base["summary"] = summary
    base["dropped_figures"] = dropped
    if not widgets:
        base["fallback_reason"] = (
            f"{names} has no numeric figures I could verify, so there is nothing to chart."
            + (f" {summary}" if summary else "")
        )
        return base

    if summary:
        widgets.insert(0, _widget("text", "About this document", content=summary, rows=[]))
    base["widgets"] = widgets

    from app.agent.dashboard_insight_service import (
        ensure_ai_insight_widget,
        refresh_ai_insight_widgets,
    )

    ensure_ai_insight_widget(base)
    refresh_ai_insight_widgets(base, llm=llm)
    return base
