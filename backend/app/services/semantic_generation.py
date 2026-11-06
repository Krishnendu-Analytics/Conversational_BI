"""Draft a semantic model from the schema when the customer has none.

Many companies have no KPI catalog, relationship sheet or business rules
written down. This module drafts them from the discovered schema (and any
data dictionary descriptions):

- ``infer_relationships`` finds joins from column names, without AI: a
  ``customer_key`` column joins to the one table that owns ``customer_key``
  (its primary key, or a table named after it such as ``dim_customer``), and
  ``customer_id`` joins to ``customers.id``.
- ``generate_model`` asks the LLM for KPIs, business rules, defaults,
  vocabulary, example questions and one-line table descriptions.

Everything drafted here goes through the same checks and preview as a
customer's own file (``app.services.semantic_model_import`` and friends):
KPI formulas must compile on one real table, rules naming missing columns
are flagged, and nothing is saved until an admin approves it.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.usage import ContextThreadPoolExecutor
from app.services.data_dictionary import DictRow, summarize_tables_with_llm
from app.services.semantic_examples import ExampleRow
from app.services.semantic_model_import import KpiRow, RelationshipRow
from app.services.semantic_rules import RuleRow, split_terms

logger = logging.getLogger(__name__)

KINDS = ("kpis", "relationships", "rules", "vocabulary", "examples", "descriptions")

_KPI_TABLES_PER_CALL = 5
_WORKERS = 4
_MAX_TABLES = 60
_MAX_COLUMNS = 40
_SCHEMA_CHARS = 24000
_MAX_OUTPUT_TOKENS = 4000
_NUMERIC = ("int", "numeric", "decimal", "float", "double", "real", "money", "number")
_KEY_SUFFIXES = ("_key", "_id", "_sk")
_TABLE_PREFIXES = ("dim_", "d_", "tbl_", "lkp_", "ref_", "master_", "mst_")


@dataclass
class GeneratedModel:
    kpis: list[KpiRow] = field(default_factory=list)
    relationships: list[RelationshipRow] = field(default_factory=list)
    rules: list[RuleRow] = field(default_factory=list)
    examples: list[ExampleRow] = field(default_factory=list)
    descriptions: list[DictRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _tables(schema: dict[str, Any]) -> list[dict]:
    return [t for t in schema.get("tables") or [] if isinstance(t, dict) and t.get("table_name")]


def _columns(table: dict) -> list[dict]:
    return [c for c in table.get("columns") or [] if isinstance(c, dict) and c.get("name")]


def _is_numeric(col: dict) -> bool:
    return any(h in str(col.get("data_type") or "").lower() for h in _NUMERIC)


def _is_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in ("id", "key") or lowered.endswith(_KEY_SUFFIXES)


# --- Relationships from column names ---------------------------------------------------------


def _stem(name: str) -> str:
    lowered = name.lower()
    for suffix in _KEY_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    return lowered


def _table_stem(table_name: str) -> str:
    lowered = table_name.lower()
    for prefix in _TABLE_PREFIXES:
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
            break
    if lowered.endswith("ies"):
        return lowered[:-3] + "y"
    if lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


def _owns(table: dict, column: str) -> bool:
    """Is ``column`` this table's own key (not a reference to another)?"""
    cols = _columns(table)
    col = next((c for c in cols if str(c["name"]).lower() == column.lower()), None)
    if col is None:
        return False
    if col.get("is_pk"):
        return True
    return _table_stem(str(table["table_name"])) == _stem(column)


def infer_relationships(schema: dict[str, Any]) -> list[RelationshipRow]:
    tables = _tables(schema)
    out: list[RelationshipRow] = []
    for table in tables:
        for col in _columns(table):
            name = str(col["name"])
            if not name.lower().endswith(_KEY_SUFFIXES) or _owns(table, name):
                continue
            owners = [t for t in tables if t is not table and _owns(t, name)]
            to_column = name
            if not owners and name.lower().endswith("_id"):
                # orders.customer_id -> customers.id
                stem = _stem(name)
                owners = [
                    t
                    for t in tables
                    if t is not table
                    and _table_stem(str(t["table_name"])) == stem
                    and any(str(c["name"]).lower() == "id" for c in _columns(t))
                ]
                to_column = "id"
            if len(owners) != 1:
                continue
            owner = owners[0]
            out.append(
                RelationshipRow(
                    from_table=str(table["table_name"]),
                    from_column=name,
                    to_table=str(owner["table_name"]),
                    to_column=next(
                        str(c["name"]) for c in _columns(owner) if str(c["name"]).lower() == to_column.lower()
                    ),
                    from_schema=str(table.get("schema_name") or ""),
                    to_schema=str(owner.get("schema_name") or ""),
                    cardinality="many-to-one",
                    description="inferred from matching column names",
                    origin="inferred",
                )
            )
    return out


# --- LLM drafting -----------------------------------------------------------------------------


def _qualified(table: dict) -> str:
    return f"{table['schema_name']}.{table['table_name']}" if table.get("schema_name") else str(table["table_name"])


def _schema_text(tables: list[dict], *, with_columns: bool = True) -> str:
    blocks: list[str] = []
    total = 0
    for table in tables[:_MAX_TABLES]:
        head = _qualified(table)
        if table.get("description"):
            head += f" — {str(table['description'])[:160]}"
        lines = [head]
        if with_columns:
            for col in _columns(table)[:_MAX_COLUMNS]:
                line = f"  {col['name']} {col.get('data_type') or ''}".rstrip()
                if col.get("is_pk"):
                    line += " pk"
                if col.get("description"):
                    line += f" — {str(col['description'])[:80]}"
                lines.append(line)
        else:
            lines.append("  columns: " + ", ".join(str(c["name"]) for c in _columns(table)[:_MAX_COLUMNS]))
        block = "\n".join(lines)
        total += len(block)
        if total > _SCHEMA_CHARS:
            break
        blocks.append(block)
    return "\n".join(blocks)


def _json(raw: str) -> dict:
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return {}
    match = re.search(r"\{[\s\S]*\}", text)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


_KPI_PROMPT = """You are a BI analyst defining KPIs for the database tables below.
For each table that holds measurable business activity, propose up to 6 useful KPIs.

Each formula MUST:
- be one SQL aggregate expression (SUM, AVG, COUNT, MIN, MAX, NULLIF, CASE WHEN) over columns of ONE table,
- reference columns as table.column using the exact names below,
- never aggregate identifier columns (names ending in _key, _id, pk) except inside COUNT,
- compute ratios/percentages as SUM(numerator) / NULLIF(SUM(denominator), 0), never AVG of a percent column.
Use only the columns listed. Do not invent columns. Skip a table if nothing meaningful can be measured.

Return ONLY JSON, no commentary:
{{"kpis": [{{"name": "...", "formula": "...", "description": "one sentence", "domain": "business area"}}]}}

TABLES:
{tables}"""


_GUIDANCE_PROMPT = """You are documenting a company database for a BI assistant that writes SQL.
Using ONLY the tables and columns below, draft:
{wanted}

Ground everything in the listed names. Do not invent tables, columns or business facts
that the names and descriptions do not support. Keep each rule to one sentence.

Return ONLY JSON, no commentary, with just the keys requested:
{{"rules": [{{"category": "...", "rule": "...", "guidance": "..."}}],
 "settings": [{{"name": "snake_case_parameter", "value": "...", "rule": "..."}}],
 "vocabulary": [{{"concept": "...", "terms": ["synonym", "..."]}}],
 "examples": [{{"question": "...", "tables": ["table_name", "..."], "domain": "..."}}]}}

TABLES:
{tables}"""

_WANTED = {
    "rules": '- "rules": up to 12 business rules the assistant must follow (which column is the default revenue, which tables are snapshots that must not be summed over time, how to treat flags and nulls, which tables must not be joined row by row).\n'
    '- "settings": up to 6 defaults (default time dimension table, default revenue metric column, units).',
    "vocabulary": '- "vocabulary": up to 20 business concepts from the table names with the words users may say for them.',
    "examples": '- "examples": up to 10 realistic business questions a manager would ask, each with the tables needed to answer it.',
}


def _looks_like_fact(table: dict) -> bool:
    """Named like a fact table, or references two or more other tables."""
    name = str(table["table_name"]).lower()
    if name.startswith(("fact_", "fct_", "f_")) or name.endswith("_fact"):
        return True
    references = [
        c for c in _columns(table)
        if _is_key(str(c["name"])) and not c.get("is_pk") and not _owns(table, str(c["name"]))
    ]
    return len(references) >= 2


def _measure_tables(tables: list[dict]) -> list[dict]:
    """Tables with measurable columns; only the fact tables when there are
    any, so KPIs describe business activity rather than master data."""
    measurable = [
        t for t in tables
        if any(_is_numeric(c) and not _is_key(str(c["name"])) and not c.get("is_pk") for c in _columns(t))
    ]
    facts = [t for t in measurable if _looks_like_fact(t)]
    return facts or measurable


def _draft_kpis(batch: list[dict], llm: Any) -> list[KpiRow] | None:
    try:
        raw = llm.complete(_KPI_PROMPT.format(tables=_schema_text(batch)), max_output_tokens=_MAX_OUTPUT_TOKENS)
    except Exception:  # noqa: BLE001 — drafting is best effort
        logger.exception("KPI drafting failed")
        return None
    items = _json(raw).get("kpis")
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name, formula = _text(item.get("name"), 120), _text(item.get("formula"), 1000)
        if name and formula:
            out.append(
                KpiRow(
                    name=name,
                    formula=formula,
                    description=_text(item.get("description"), 2000),
                    domain=_text(item.get("domain"), 120),
                    origin="ai",
                )
            )
    return out


def _draft_guidance(tables: list[dict], wanted: set[str], llm: Any) -> dict | None:
    lines = "\n".join(_WANTED[k] for k in ("rules", "vocabulary", "examples") if k in wanted)
    prompt = _GUIDANCE_PROMPT.format(wanted=lines, tables=_schema_text(tables, with_columns=True))
    try:
        raw = llm.complete(prompt, max_output_tokens=_MAX_OUTPUT_TOKENS)
    except Exception:  # noqa: BLE001
        logger.exception("guidance drafting failed")
        return None
    data = _json(raw)
    return data or None


def _guidance_rows(data: dict, wanted: set[str]) -> tuple[list[RuleRow], list[ExampleRow]]:
    rules: list[RuleRow] = []
    if "rules" in wanted:
        for item in data.get("rules") or []:
            if isinstance(item, dict) and _text(item.get("rule"), 2000):
                rules.append(
                    RuleRow(
                        "rule",
                        category=_text(item.get("category"), 120),
                        rule=_text(item.get("rule"), 2000),
                        guidance=_text(item.get("guidance"), 2000),
                        origin="ai",
                    )
                )
        for item in data.get("settings") or []:
            name, value = (_text(item.get("name"), 200), _text(item.get("value"), 500)) if isinstance(item, dict) else ("", "")
            if name and value:
                rules.append(
                    RuleRow(
                        "setting",
                        name=name,
                        rule=f"{name} = {value}",
                        guidance=_text(item.get("rule"), 2000),
                        origin="ai",
                    )
                )
    if "vocabulary" in wanted:
        for item in data.get("vocabulary") or []:
            if not isinstance(item, dict):
                continue
            concept = _text(item.get("concept"), 200)
            raw_terms = item.get("terms")
            terms_text = ";".join(str(t) for t in raw_terms) if isinstance(raw_terms, list) else str(raw_terms or "")
            terms = [t for t in split_terms(terms_text) if t.lower() != concept.lower()]
            if concept and terms:
                rules.append(RuleRow("vocabulary", name=concept, rule=concept, terms=terms, origin="ai"))
    examples: list[ExampleRow] = []
    if "examples" in wanted:
        for item in data.get("examples") or []:
            if not isinstance(item, dict) or not _text(item.get("question"), 500):
                continue
            tables = item.get("tables")
            examples.append(
                ExampleRow(
                    question=_text(item.get("question"), 500),
                    tables=[_text(t, 200) for t in tables] if isinstance(tables, list) else [],
                    domain=_text(item.get("domain"), 120),
                    origin="ai",
                )
            )
    return rules, examples


def _draft_descriptions(schema: dict[str, Any], llm: Any) -> tuple[list[DictRow], list[tuple[int | None, str]]]:
    """One-line summaries for tables with no description yet."""
    tables = [t for t in _tables(schema) if not t.get("description")][:_MAX_TABLES]
    rows = [
        DictRow(str(t.get("schema_name") or ""), str(t["table_name"]), str(c["name"]), str(c.get("description") or ""))
        for t in tables
        for c in _columns(t)[:_MAX_COLUMNS]
    ]
    if not rows:
        return [], []
    return summarize_tables_with_llm(schema, rows, set(), llm)


def generate_model(schema: dict[str, Any], kinds: set[str], llm: Any) -> GeneratedModel:
    """Draft the requested kinds. LLM failures become notes, not errors."""
    out = GeneratedModel()
    tables = _tables(schema)
    if not tables:
        return out
    if "relationships" in kinds:
        out.relationships = infer_relationships(schema)

    jobs: dict[str, Any] = {}
    guidance_kinds = kinds & {"rules", "vocabulary", "examples"}
    with ContextThreadPoolExecutor(max_workers=_WORKERS) as pool:
        if "kpis" in kinds:
            measure = _measure_tables(tables)[:_MAX_TABLES]
            batches = [measure[i : i + _KPI_TABLES_PER_CALL] for i in range(0, len(measure), _KPI_TABLES_PER_CALL)]
            jobs["kpis"] = [pool.submit(_draft_kpis, b, llm) for b in batches]
        if guidance_kinds:
            jobs["guidance"] = pool.submit(_draft_guidance, tables, guidance_kinds, llm)
        if "descriptions" in kinds:
            jobs["descriptions"] = pool.submit(_draft_descriptions, schema, llm)

        if "kpis" in jobs:
            results = [f.result() for f in jobs["kpis"]]
            seen: set[str] = set()
            for batch in results:
                for kpi in batch or []:
                    if kpi.name.lower() not in seen:
                        seen.add(kpi.name.lower())
                        out.kpis.append(kpi)
            if results and all(r is None for r in results):
                out.notes.append("AI could not draft KPIs")
        if "guidance" in jobs:
            data = jobs["guidance"].result()
            if data is None:
                out.notes.append("AI could not draft rules, vocabulary or example questions")
            else:
                out.rules, out.examples = _guidance_rows(data, guidance_kinds)
        if "descriptions" in jobs:
            out.descriptions, failed = jobs["descriptions"].result()
            if failed:
                out.notes.append("AI could not describe some tables")
    return out
