"""Recognise what each sheet of an uploaded workbook contains.

The importers find sheets by their header names ("kpi_name", "formula",
"table_name", ...). Customers name columns however they like ("Measure",
"Calc", "Physical Table"), so for any sheet no importer recognises this
module asks the LLM what kind of sheet it is and which header means which
field. The answer is only trusted when every header it names really is in
the sheet and the kind's required fields are present; the header row is
then rewritten to the canonical names and the normal, deterministic
importers read it. The LLM never supplies data, only the column mapping.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.services import data_dictionary, semantic_examples, semantic_rules
from app.services.file_import import SourceDocument, clean_cell

logger = logging.getLogger(__name__)

_SAMPLE_ROWS = 8
_CELL_CHARS = 60
_MAX_SHEETS = 12
_MAX_OUTPUT_TOKENS = 3000

# kind -> field -> canonical header the deterministic parser recognises.
CANONICAL: dict[str, dict[str, str]] = {
    "dictionary": {
        "schema": "schema",
        "table": "table_name",
        "column": "column_name",
        "description": "description",
        "table_description": "table_description",
    },
    "kpi": {
        "name": "kpi_name",
        "formula": "formula",
        "description": "description",
        "domain": "domain",
        "synonyms": "synonyms",
    },
    "relationship": {
        "from_table": "from_table",
        "from_column": "from_column",
        "to_table": "to_table",
        "to_column": "to_column",
        "join": "join_rule",
        "cardinality": "cardinality",
        "description": "description",
    },
    "rule": {"rule": "business_rule", "name": "rule_id", "category": "category", "guidance": "guardrail"},
    "setting": {"name": "parameter", "value": "value", "rule": "agent_rule"},
    "vocabulary": {"concept": "concept", "terms": "synonyms"},
    "examples": {"question": "example_question", "tables": "tables", "domain": "domain", "notes": "notes"},
}

_REQUIRED: dict[str, list[set[str]]] = {
    # Each set is one acceptable combination of required fields.
    "dictionary": [{"table", "description"}, {"table", "table_description"}],
    "kpi": [{"name", "formula"}],
    "relationship": [{"from_table", "from_column", "to_table", "to_column"}, {"join"}],
    "rule": [{"rule"}],
    "setting": [{"name", "value"}],
    "vocabulary": [{"concept", "terms"}],
    "examples": [{"question"}],
}

_PROMPT = """You are preparing spreadsheet sheets for import into a BI assistant's semantic layer.
For each sheet below, decide what it contains and map its header cells to fields.

Kinds and their fields (required fields marked *):
- dictionary: meaning of tables/columns. table*, column, schema, description*, table_description
- kpi: KPI or metric definitions. name*, formula*, description, domain, synonyms
- relationship: how tables join. from_table*, from_column*, to_table*, to_column* (or a single join* column like "a.x = b.y"), cardinality, description
- rule: business rules / guidance. rule*, name (an id), category, guidance
- setting: parameters/defaults. name*, value*, rule
- vocabulary: business terms and their synonyms. concept*, terms*
- examples: sample business questions. question*, tables, domain, notes
- data: actual business records (transactions, customers, amounts) — not metadata
- other: anything else (readme, notes, charts)

Rules:
- "header_row" is the 0-based row number of the header row as listed.
- Map a field only to a header cell copied EXACTLY from that header row.
- Leave out fields you are not sure about. Use "other" when unsure of the kind.

Return ONLY JSON, no commentary:
{{"sheets": [{{"sheet": <sheet number>, "kind": "<kind>", "header_row": <n>, "columns": {{"<field>": "<exact header text>"}}}}]}}

SHEETS:
{sheets}"""


@dataclass
class SheetNote:
    sheet: str
    message: str


@dataclass
class MappingResult:
    source: SourceDocument
    notes: list[SheetNote] = field(default_factory=list)


def recognised(rows: list[list[object]]) -> bool:
    """True when a deterministic importer already understands the sheet."""
    from app.services import semantic_model_import as smi

    return any(
        detect(rows) is not None
        for detect in (
            smi._kpi_header,
            smi._relationship_header,
            semantic_rules._sheet_header,
            data_dictionary._find_header,
            semantic_examples._examples_header,
        )
    )


def _sheet_block(number: int, name: str, rows: list[list[object]]) -> str:
    lines = [f"Sheet {number}: {name!r}"]
    for i, row in enumerate(rows[: _SAMPLE_ROWS + 2]):
        cells = [clean_cell(c)[:_CELL_CHARS] for c in row]
        while cells and not cells[-1]:
            cells.pop()
        lines.append(f"  row {i}: " + " | ".join(cells))
    return "\n".join(lines)


def _parse_response(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return []
    match = re.search(r"\{[\s\S]*\}", text)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return []
    items = data.get("sheets") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _apply(rows: list[list[object]], item: dict) -> tuple[list[list[object]] | None, str]:
    kind = str(item.get("kind") or "")
    if kind not in CANONICAL:
        return None, ""
    try:
        header_row = int(item.get("header_row", 0))
    except (TypeError, ValueError):
        return None, "unclear header row"
    if not 0 <= header_row < min(len(rows), 10):
        return None, "unclear header row"
    headers = [clean_cell(c) for c in rows[header_row]]
    columns = item.get("columns") if isinstance(item.get("columns"), dict) else {}
    mapping: dict[int, str] = {}
    labels: list[str] = []
    for field_name, header in columns.items():
        canonical = CANONICAL[kind].get(str(field_name))
        if canonical is None:
            continue
        header_text = clean_cell(header)
        if header_text not in headers:
            return None, f'AI named a column "{header_text}" that is not in the sheet'
        index = headers.index(header_text)
        if index in mapping:
            continue
        mapping[index] = canonical
        labels.append(f"{header_text} → {field_name}")
    fields = {f for f, c in CANONICAL[kind].items() if c in mapping.values()}
    if not any(req <= fields for req in _REQUIRED[kind]):
        return None, "required columns could not be identified"
    new_header = [mapping.get(i, "") for i in range(len(headers))]
    return [new_header] + [list(r) for r in rows[header_row + 1 :]], ", ".join(labels)


_KIND_LABEL = {
    "dictionary": "data dictionary",
    "kpi": "KPI catalog",
    "relationship": "relationships",
    "rule": "business rules",
    "setting": "settings",
    "vocabulary": "vocabulary",
    "examples": "example questions",
}


def map_unrecognised_sheets(source: SourceDocument, llm: Any) -> MappingResult:
    """Rewrite the header row of sheets only the LLM can interpret."""
    unknown = [i for i, rows in enumerate(source.sheets) if not recognised(rows)][:_MAX_SHEETS]
    result = MappingResult(source=source)
    if not unknown or llm is None:
        return result
    prompt = _PROMPT.format(
        sheets="\n\n".join(_sheet_block(i + 1, source.name(i), source.sheets[i]) for i in unknown)
    )
    try:
        raw = llm.complete(prompt, max_output_tokens=_MAX_OUTPUT_TOKENS)
    except Exception:  # noqa: BLE001 — mapping is best effort
        logger.exception("sheet mapping failed")
        result.notes.append(SheetNote("", "AI could not read the unrecognised sheets"))
        return result

    sheets = [list(rows) for rows in source.sheets]
    for item in _parse_response(raw):
        try:
            index = int(item.get("sheet", 0)) - 1
        except (TypeError, ValueError):
            continue
        if index not in unknown:
            continue
        name = source.name(index)
        kind = str(item.get("kind") or "")
        if kind == "data":
            result.notes.append(SheetNote(name, "looks like business data, not metadata; skipped"))
            continue
        rewritten, detail = _apply(source.sheets[index], item)
        if rewritten is None:
            if detail:
                result.notes.append(SheetNote(name, f"not imported: {detail}"))
            continue
        if not recognised(rewritten):
            result.notes.append(SheetNote(name, "not imported: columns could not be matched"))
            continue
        sheets[index] = rewritten
        result.notes.append(SheetNote(name, f"read as {_KIND_LABEL[kind]} by AI ({detail})"))
    result.source = SourceDocument(sheets=sheets, names=list(source.names))
    return result
