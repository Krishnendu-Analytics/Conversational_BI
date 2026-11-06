"""Import KPIs and table relationships from an uploaded CSV / Excel file.

Customers often keep a "semantic model" workbook next to their data: a KPI
catalog (name + formula) and a relationship catalog (which column joins to
which). The data dictionary importer (``app.services.data_dictionary``)
takes the descriptions from such a file; this module takes the rest:

1. ``parse_semantic_model`` finds KPI and relationship sheets by their
   headers, whatever else the workbook contains.
2. ``resolve_kpi`` turns a formula into a safe SQL expression over ONE real
   table: every column must exist, only whitelisted SQL functions are
   allowed, bare column names are qualified, and ``SUM(x WHERE cond)`` is
   rewritten to ``SUM(CASE WHEN cond THEN x END)``. Anything it cannot
   ground (``LATEST(...)``, free text, columns from two fact tables) is
   reported, never guessed. Formulas also get sanity warnings (averaging a
   percentage, summing a price, dividing without NULLIF).
3. ``resolve_relationship`` checks both join columns exist and look
   type-compatible; many-to-many joins are refused.
4. Business rules, agent settings and vocabulary sheets are read by
   ``app.services.semantic_rules`` from whatever sheets are left.
5. Saving creates KPIs as ``suggested`` metrics (confirmed on the Semantic
   page like every other suggestion) and relationships as ``confirmed``
   join paths — the admin reviewed each one in the preview, and there is no
   other place to confirm them. ``apply_relationships_to_schema`` then adds
   them to the schema's foreign keys, so the SQL prompt and the join planner
   can use joins the database itself never declared.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm.usage import ContextThreadPoolExecutor
from app.models import Metric, SemanticRelationship
from app.services.file_import import ImportFileError, SourceDocument, clean_cell, norm_header
from app.services.semantic_discovery import (
    existing_semantic_names,
    find_metric_by_source_ref,
    physical_parts_from_ref,
)
from app.services.semantic_identity import encode_synonyms, parse_synonyms, unique_name
from app.services.semantic_rules import RuleRow, parse_rule_sheets

MAX_FORMULA_LEN = 1000
_VALIDATION_WORKERS = 4

# Validation query runner: (sql) -> None, raising when the database rejects it.
SqlValidator = Callable[[str], None]


class SemanticModelError(ImportFileError):
    """The file has no usable KPI, relationship or rule sheet."""


# --- Parsing ------------------------------------------------------------------

_KPI_NAME_HEADERS = {
    "kpi name", "kpi", "metric name", "metric", "measure name", "measure",
    "kpi title", "indicator", "indicator name",
}
_KPI_FORMULA_HEADERS = {
    "formula", "kpi formula", "metric formula", "expression", "metric expression",
    "sql expression", "sql formula", "calculation", "sql", "measure formula",
}
_KPI_DESCRIPTION_WORDS = ("description", "definition", "business rule", "meaning", "comment")
_DOMAIN_HEADERS = {"domain", "business domain", "subject area", "category", "area"}
_SYNONYM_WORDS = ("synonym", "alias", "also known as")

_FROM_TABLE_HEADERS = {
    "from table", "source table", "child table", "fact table", "from entity",
    "many table", "foreign table",
}
_FROM_COLUMN_HEADERS = {
    "from column", "source column", "child column", "foreign key", "fk column",
    "fk", "from key", "from field", "source field",
}
_TO_TABLE_HEADERS = {
    "to table", "target table", "parent table", "referenced table", "referred table",
    "dimension table", "lookup table", "to entity", "one table", "primary table",
}
_TO_COLUMN_HEADERS = {
    "to column", "target column", "parent column", "referenced column",
    "referred column", "primary key", "pk column", "pk", "to key", "to field",
    "target field",
}
_CARDINALITY_HEADERS = {"cardinality", "relationship type", "relationship", "join type"}
_JOIN_HEADERS = {"join rule", "join condition", "join", "join on", "join key", "join expression"}
_REL_DESCRIPTION_WORDS = ("description", "relationship rule", "business rule", "comment", "meaning")


@dataclass
class KpiRow:
    name: str
    formula: str
    description: str = ""
    domain: str = ""
    synonyms: list[str] = field(default_factory=list)
    origin: str = "import"  # import | ai
    row: int | None = None


@dataclass
class RelationshipRow:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    from_schema: str = ""
    to_schema: str = ""
    cardinality: str = ""
    description: str = ""
    origin: str = "import"  # import | inferred
    row: int | None = None


@dataclass
class ParsedModel:
    kpis: list[KpiRow] = field(default_factory=list)
    relationships: list[RelationshipRow] = field(default_factory=list)
    rules: list[RuleRow] = field(default_factory=list)
    skipped: list[tuple[int | None, str]] = field(default_factory=list)


def _header_index(headers: list[str], exact: set[str]) -> int | None:
    return next((i for i, h in enumerate(headers) if h in exact), None)


def _header_containing(headers: list[str], words: Iterable[str], taken: set[int]) -> int | None:
    return next(
        (
            i
            for i, h in enumerate(headers)
            if i not in taken and h and any(w in h for w in words)
        ),
        None,
    )


def _kpi_header(rows: list[list[object]]) -> tuple[int, dict[str, int]] | None:
    for idx, row in enumerate(rows[:10]):
        headers = [norm_header(c) for c in row]
        name = _header_index(headers, _KPI_NAME_HEADERS)
        formula = _header_index(headers, _KPI_FORMULA_HEADERS)
        if name is None or formula is None:
            continue
        cols = {"name": name, "formula": formula}
        taken = {name, formula}
        for key, index in (
            ("domain", _header_index(headers, _DOMAIN_HEADERS)),
            ("synonyms", _header_containing(headers, _SYNONYM_WORDS, taken)),
        ):
            if index is not None and index not in taken:
                cols[key] = index
                taken.add(index)
        description = _header_containing(headers, _KPI_DESCRIPTION_WORDS, taken)
        if description is not None:
            cols["description"] = description
        return idx, cols
    return None


def _relationship_header(rows: list[list[object]]) -> tuple[int, dict[str, int]] | None:
    for idx, row in enumerate(rows[:10]):
        headers = [norm_header(c) for c in row]
        cols = {
            key: index
            for key, index in (
                ("from_table", _header_index(headers, _FROM_TABLE_HEADERS)),
                ("from_column", _header_index(headers, _FROM_COLUMN_HEADERS)),
                ("to_table", _header_index(headers, _TO_TABLE_HEADERS)),
                ("to_column", _header_index(headers, _TO_COLUMN_HEADERS)),
                ("cardinality", _header_index(headers, _CARDINALITY_HEADERS)),
                ("join", _header_index(headers, _JOIN_HEADERS)),
            )
            if index is not None
        }
        full = all(k in cols for k in ("from_table", "from_column", "to_table", "to_column"))
        if not full and "join" not in cols:
            continue
        description = _header_containing(headers, _REL_DESCRIPTION_WORDS, set(cols.values()))
        if description is not None:
            cols["description"] = description
        return idx, cols
    return None


_JOIN_SIDE = r"((?:[A-Za-z_][\w]*\.){1,2}[A-Za-z_][\w]*)"
_JOIN_RE = re.compile(rf"^\s*{_JOIN_SIDE}\s*=\s*{_JOIN_SIDE}\s*$")


def _split_ref(ref: str) -> tuple[str, str, str]:
    """"schema.table.column" / "table.column" -> (schema, table, column)."""
    parts = ref.split(".")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", parts[0], parts[1]


def _split_table(value: str) -> tuple[str, str]:
    """"schema.table" -> (schema, table); "table" -> ("", table)."""
    if "." in value:
        schema, _, table = value.rpartition(".")
        return schema, table
    return "", value


def parse_semantic_model(source: SourceDocument) -> ParsedModel:
    """Find KPI, relationship and rule sheets anywhere in the workbook."""
    result = ParsedModel()
    seen_kpis: set[str] = set()
    seen_rels: set[tuple[str, ...]] = set()
    unused: list[list[list[object]]] = []

    for rows in source.sheets:
        kpi_header = _kpi_header(rows)
        rel_header = None if kpi_header else _relationship_header(rows)
        if kpi_header is None and rel_header is None:
            unused.append(rows)
            continue
        start, cols = kpi_header or rel_header  # type: ignore[misc]

        def cell(row: list[object], name: str) -> str:
            i = cols.get(name)
            return clean_cell(row[i]) if i is not None and i < len(row) else ""

        for idx in range(start + 1, len(rows)):
            row = rows[idx]
            if not any(clean_cell(c) for c in row):
                continue
            row_no = idx + 1
            if kpi_header:
                name, formula = cell(row, "name"), cell(row, "formula")
                if not name or not formula:
                    result.skipped.append((row_no, "KPI row needs both a name and a formula"))
                    continue
                if len(formula) > MAX_FORMULA_LEN:
                    result.skipped.append((row_no, f'KPI "{name}": formula is too long'))
                    continue
                if name.lower() in seen_kpis:
                    continue
                seen_kpis.add(name.lower())
                synonyms = [
                    s.strip()
                    for s in re.split(r"[;,|]", cell(row, "synonyms"))
                    if s.strip() and s.strip().lower() != name.lower()
                ]
                result.kpis.append(
                    KpiRow(
                        name=name[:120],
                        formula=formula,
                        description=cell(row, "description")[:2000],
                        domain=cell(row, "domain")[:120],
                        synonyms=synonyms,
                        row=row_no,
                    )
                )
                continue

            from_schema, from_table = _split_table(cell(row, "from_table"))
            to_schema, to_table = _split_table(cell(row, "to_table"))
            from_column, to_column = cell(row, "from_column"), cell(row, "to_column")
            if not (from_table and from_column and to_table and to_column):
                match = _JOIN_RE.match(cell(row, "join"))
                if not match:
                    result.skipped.append(
                        (row_no, "relationship row needs from/to table and column, or a join like a.x = b.y")
                    )
                    continue
                from_schema, from_table, from_column = _split_ref(match.group(1))
                to_schema, to_table, to_column = _split_ref(match.group(2))
            key = (from_schema.lower(), from_table.lower(), from_column.lower(),
                   to_schema.lower(), to_table.lower(), to_column.lower())
            if key in seen_rels:
                continue
            seen_rels.add(key)
            result.relationships.append(
                RelationshipRow(
                    from_table=from_table,
                    from_column=from_column,
                    to_table=to_table,
                    to_column=to_column,
                    from_schema=from_schema,
                    to_schema=to_schema,
                    cardinality=cell(row, "cardinality"),
                    description=cell(row, "description")[:2000],
                    row=row_no,
                )
            )

    rules, rule_skips, _used = parse_rule_sheets(unused)
    result.rules = rules
    result.skipped += rule_skips

    if not result.kpis and not result.relationships and not result.rules:
        raise SemanticModelError(
            "Couldn't find a KPI sheet (kpi_name, formula), a relationship sheet "
            "(from_table, from_column, to_table, to_column), a rules sheet (business_rule), "
            "a settings sheet (parameter, value) or a vocabulary sheet (concept, synonyms) "
            "in this file."
        )
    return result


# --- Schema lookup ------------------------------------------------------------------


class _Schema:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.tables: list[dict] = [
            t for t in schema.get("tables") or [] if isinstance(t, dict) and t.get("table_name")
        ]
        self.by_name: dict[str, list[dict]] = {}
        for t in self.tables:
            self.by_name.setdefault(str(t["table_name"]).lower(), []).append(t)

    @staticmethod
    def qualified(table: dict) -> str:
        schema_name = str(table.get("schema_name") or "")
        name = str(table["table_name"])
        return f"{schema_name}.{name}" if schema_name else name

    def table(self, schema_name: str, table_name: str) -> tuple[dict | None, str | None]:
        candidates = self.by_name.get(table_name.lower()) or []
        if schema_name:
            candidates = [
                t for t in candidates if str(t.get("schema_name") or "").lower() == schema_name.lower()
            ]
        if not candidates:
            label = f"{schema_name}.{table_name}" if schema_name else table_name
            return None, f'table "{label}" not found in this connection'
        if len(candidates) > 1:
            return None, f'table "{table_name}" exists in several schemas; write it as schema.table'
        return candidates[0], None

    @staticmethod
    def column(table: dict, name: str) -> dict | None:
        return next(
            (
                c
                for c in table.get("columns") or []
                if isinstance(c, dict) and str(c.get("name") or "").lower() == name.lower()
            ),
            None,
        )

    def tables_with_column(self, name: str) -> list[dict]:
        return [t for t in self.tables if self.column(t, name) is not None]


# --- KPI formulas ------------------------------------------------------------------

_AGGREGATES = {"SUM", "AVG", "COUNT", "MIN", "MAX"}
_FUNCTIONS = _AGGREGATES | {"NULLIF", "COALESCE", "ABS", "ROUND"}
_KEYWORDS = {
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IS", "NULL",
    "DISTINCT", "IN", "BETWEEN", "LIKE", "TRUE", "FALSE",
}
_TOKEN_RE = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<str>'[^']*')"
    r"|(?P<ident>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){0,2})"
    r"|(?P<op><=|>=|<>|!=|[-+*/(),=<>%])"
)
_DAX_REF_RE = re.compile(r"([A-Za-z_][\w]*)\s*\[\s*([A-Za-z_][\w]*)\s*\]")
_AGG_WHERE_RE = re.compile(
    r"\b(SUM|AVG|COUNT|MIN|MAX)\s*\(\s*([^()]+?)\s+WHERE\s+([^()]+?)\s*\)", re.IGNORECASE
)
_PERCENT_COLUMN = re.compile(r"(percent|pct|ratio|rate|margin|yield|share)", re.IGNORECASE)
_PRICE_COLUMN = re.compile(r"(^|_)(price|rate|percent|pct|ratio)(_|$)", re.IGNORECASE)
_NUMERIC_TYPES = ("int", "numeric", "decimal", "float", "double", "real", "money", "number", "serial")
_SINGLE_AGG_RE = re.compile(r"^(SUM|AVG|COUNT|MIN|MAX)\(([\w.]+)\)$")


@dataclass
class ResolvedKpi:
    kpi: KpiRow
    expression: str = ""
    table: str = ""  # qualified table the formula reads
    source_column_ref: str | None = None  # set for a plain AGG(column)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def _normalize_formula(formula: str) -> str:
    text = _DAX_REF_RE.sub(r"\1.\2", formula.strip())
    # "SUM(x WHERE cond)" is a common business shorthand for a filtered sum.
    while True:
        rewritten = _AGG_WHERE_RE.sub(
            lambda m: f"{m.group(1)}(CASE WHEN {m.group(3)} THEN {m.group(2)} END)", text
        )
        if rewritten == text:
            return text
        text = rewritten


def _is_numeric(col: dict) -> bool:
    dtype = str(col.get("data_type") or "").lower()
    return any(h in dtype for h in _NUMERIC_TYPES)


def resolve_kpi(kpi: KpiRow, schema: dict[str, Any]) -> ResolvedKpi:
    """Ground a KPI formula in exactly one real table; never guess."""
    out = ResolvedKpi(kpi=kpi)
    index = _Schema(schema)
    text = _normalize_formula(kpi.formula)

    tokens: list[tuple[str, str, int, int]] = []  # (kind, text, start, end)
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            out.error = f'unsupported character "{text[pos]}" in formula'
            return out
        kind = m.lastgroup or ""
        if kind != "ws":
            tokens.append((kind, m.group(0), m.start(), m.end()))
        pos = m.end()

    depth = 0
    for _, tok, _, _ in tokens:
        depth += tok == "("
        depth -= tok == ")"
        if depth < 0:
            break
    if depth != 0:
        out.error = "unbalanced parentheses"
        return out

    # Classify identifiers: function, keyword or column reference.
    columns: list[tuple[int, str, str, str]] = []  # (token index, schema, table, column)
    has_aggregate = False
    previous_was_term = False
    for i, (kind, tok, _, _) in enumerate(tokens):
        if kind != "ident":
            previous_was_term = kind in ("num", "str") or tok == ")"
            continue
        upper = tok.upper()
        next_tok = tokens[i + 1][1] if i + 1 < len(tokens) else ""
        if next_tok == "(":
            if upper not in _FUNCTIONS:
                if upper in ("LATEST", "LAST", "FIRST", "LASTNONBLANK", "LAST_VALUE"):
                    out.error = (
                        f"{upper}() is not a SQL aggregate; point-in-time KPIs (latest "
                        "snapshot) need a manual definition"
                    )
                else:
                    out.error = f"{upper}() is not a supported function"
                return out
            has_aggregate = has_aggregate or upper in _AGGREGATES
            previous_was_term = False
            continue
        if upper in _KEYWORDS:
            previous_was_term = False
            continue
        if previous_was_term:
            out.error = (
                "formula contains plain text, not column names "
                f'(near "{tok}"); rewrite it with real columns'
            )
            return out
        parts = tok.split(".")
        schema_name, table_name, column = (
            (parts[0], parts[1], parts[2]) if len(parts) == 3
            else ("", parts[0], parts[1]) if len(parts) == 2
            else ("", "", parts[0])
        )
        columns.append((i, schema_name, table_name, column))
        previous_was_term = True

    if not columns:
        out.error = "formula does not reference any column"
        return out
    if not has_aggregate:
        out.error = "formula needs an aggregate such as SUM(...), AVG(...) or COUNT(...)"
        return out

    # Qualified references pin their table; bare columns must then fit it.
    explicit: dict[str, dict] = {}
    for _, schema_name, table_name, column in columns:
        if not table_name:
            continue
        table, reason = index.table(schema_name, table_name)
        if table is None:
            out.error = reason
            return out
        if index.column(table, column) is None:
            out.error = f'column "{column}" not found in table "{table["table_name"]}"'
            return out
        explicit[index.qualified(table)] = table

    bare = [c for _, _, t, c in columns if not t]
    if len(explicit) > 1:
        chosen = None
    elif explicit:
        chosen = next(iter(explicit.values()))
    else:
        chosen = None
        if bare:
            candidate_sets = []
            for column in bare:
                found = index.tables_with_column(column)
                if not found:
                    out.error = f'"{column}" is not a column in this connection'
                    return out
                candidate_sets.append({index.qualified(t) for t in found})
            common = set.intersection(*candidate_sets)
            if len(common) == 1:
                chosen = next(t for t in index.tables if index.qualified(t) in common)
            elif not common:
                out.error = (
                    "formula uses columns from several tables; cross-table KPIs are not "
                    "imported automatically (joining two fact tables row by row gives wrong totals)"
                )
                return out
            else:
                out.error = (
                    f'columns {", ".join(sorted(set(bare)))} exist in several tables '
                    f'({", ".join(sorted(common)[:4])}); write them as table.column'
                )
                return out

    if chosen is None:
        out.error = (
            "formula uses columns from several tables; cross-table KPIs are not "
            "imported automatically (joining two fact tables row by row gives wrong totals)"
        )
        return out
    for column in bare:
        if index.column(chosen, column) is None:
            out.error = f'column "{column}" not found in table "{chosen["table_name"]}"'
            return out

    qualified = index.qualified(chosen)
    replacements: dict[int, str] = {}
    for i, _, _, column in columns:
        replacements[i] = f"{qualified}.{index.column(chosen, column)['name']}"  # type: ignore[index]
    for i, (kind, tok, _, _) in enumerate(tokens):
        if kind == "ident" and i not in replacements:
            replacements[i] = tok.upper()

    pieces: list[str] = []
    cursor = 0
    for i, (_, tok, start, end) in enumerate(tokens):
        pieces.append(text[cursor:start])
        pieces.append(replacements.get(i, tok))
        cursor = end
    pieces.append(text[cursor:])
    expression = re.sub(r"\s+", " ", "".join(pieces)).strip()
    expression = re.sub(r"\(\s+", "(", expression)
    expression = re.sub(r"\s+\)", ")", expression)

    out.expression = expression
    out.table = qualified
    single = _SINGLE_AGG_RE.match(expression)
    if single:
        out.source_column_ref = single.group(2)
    out.warnings = _formula_warnings(expression, chosen, index)
    return out


def _formula_warnings(expression: str, table: dict, index: _Schema) -> list[str]:
    warnings: list[str] = []
    for agg, ref in re.findall(r"\b(SUM|AVG)\(([\w.]+)\)", expression):
        column_name = ref.rsplit(".", 1)[-1]
        col = index.column(table, column_name) or {}
        if agg == "AVG" and _PERCENT_COLUMN.search(column_name):
            warnings.append(
                f"AVG({column_name}) averages row-level percentages; a weighted "
                "SUM(numerator) / SUM(denominator) is usually correct"
            )
        if agg == "SUM" and _PRICE_COLUMN.search(column_name):
            warnings.append(f"SUM({column_name}) adds up prices or rates, which is rarely meaningful")
        if col and not _is_numeric(col) and agg in ("SUM", "AVG"):
            warnings.append(f"{column_name} is not a numeric column")
    if "/" in expression and "NULLIF" not in expression.upper():
        warnings.append("division without NULLIF(..., 0) fails when the denominator is zero")
    return warnings


def validate_kpi_sql(resolved: ResolvedKpi, validator: SqlValidator | None) -> None:
    """Compile the expression on the real database without reading rows."""
    if validator is None or resolved.error:
        return
    try:
        validator(f"SELECT {resolved.expression} AS kpi_value FROM {resolved.table} WHERE 1 = 0")
    except Exception as exc:  # noqa: BLE001 — any database rejection is reported
        message = " ".join(str(exc).split())[:300] or exc.__class__.__name__
        resolved.error = f"the database rejected this formula: {message}"


# --- Relationships ---------------------------------------------------------------------

_MANY_TO_ONE = {"many to one", "n:1", "m:1", "*:1", "many-to-one", "many_to_one", "n-1"}
_ONE_TO_MANY = {"one to many", "1:n", "1:m", "1:*", "one-to-many", "one_to_many", "1-n"}
_ONE_TO_ONE = {"one to one", "1:1", "one-to-one", "one_to_one"}
_MANY_TO_MANY = {"many to many", "n:m", "m:n", "*:*", "many-to-many", "many_to_many", "n:n"}


@dataclass
class ResolvedRelationship:
    rel: RelationshipRow
    from_table: str = ""  # qualified
    from_column: str = ""
    to_table: str = ""  # qualified
    to_column: str = ""
    to_schema: str = ""
    relationship_type: str = "many_to_one"
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.from_table.lower(),
            self.from_column.lower(),
            self.to_table.lower(),
            self.to_column.lower(),
        )

    def fk_json(self) -> str:
        return json.dumps(
            {
                "column": self.from_column,
                "referred_schema": self.to_schema or None,
                "referred_table": self.to_table,
                "referred_column": self.to_column,
                "from_table": self.from_table,
            }
        )


def resolve_relationship(rel: RelationshipRow, schema: dict[str, Any]) -> ResolvedRelationship:
    out = ResolvedRelationship(rel=rel)
    cardinality = " ".join(rel.cardinality.lower().replace("-", " ").replace("_", " ").split())
    raw = rel.cardinality.lower().strip()
    from_side = (rel.from_schema, rel.from_table, rel.from_column)
    to_side = (rel.to_schema, rel.to_table, rel.to_column)
    if cardinality in _MANY_TO_MANY or raw in _MANY_TO_MANY:
        out.error = "many-to-many joins duplicate rows and are not imported"
        return out
    if cardinality in _ONE_TO_MANY or raw in _ONE_TO_MANY:
        # Store every relationship in many-to-one direction.
        from_side, to_side = to_side, from_side
    elif cardinality in _ONE_TO_ONE or raw in _ONE_TO_ONE:
        out.relationship_type = "one_to_one"
    elif cardinality and cardinality not in _MANY_TO_ONE and raw not in _MANY_TO_ONE:
        out.warnings.append(f'unknown cardinality "{rel.cardinality}"; treated as many-to-one')

    index = _Schema(schema)
    resolved: list[tuple[dict, dict]] = []
    for schema_name, table_name, column_name in (from_side, to_side):
        table, reason = index.table(schema_name, table_name)
        if table is None:
            out.error = reason
            return out
        col = index.column(table, column_name)
        if col is None:
            out.error = f'column "{column_name}" not found in table "{table["table_name"]}"'
            return out
        resolved.append((table, col))

    (from_t, from_c), (to_t, to_c) = resolved
    if index.qualified(from_t) == index.qualified(to_t):
        out.error = "a table cannot join to itself here"
        return out
    out.from_table, out.from_column = index.qualified(from_t), str(from_c["name"])
    out.to_table, out.to_column = index.qualified(to_t), str(to_c["name"])
    out.to_schema = str(to_t.get("schema_name") or "")
    if from_c.get("data_type") and to_c.get("data_type") and _is_numeric(from_c) != _is_numeric(to_c):
        out.warnings.append(
            f"column types differ ({from_c['data_type']} vs {to_c['data_type']}); check the join"
        )
    return out


def _schema_fk_keys(schema: dict[str, Any]) -> set[tuple[str, str, str, str]]:
    """(from, column, to, referred column) of FKs the database itself declares."""
    index = _Schema(schema)
    keys: set[tuple[str, str, str, str]] = set()
    for table in index.tables:
        for fk in table.get("foreign_keys") or []:
            if not isinstance(fk, dict) or fk.get("source") == "semantic":
                continue
            ref_schema = fk.get("referred_schema") or table.get("schema_name") or ""
            ref_table = str(fk.get("referred_table") or "")
            if "." not in ref_table and ref_schema:
                ref_table = f"{ref_schema}.{ref_table}"
            keys.add(
                (
                    index.qualified(table).lower(),
                    str(fk.get("column") or "").lower(),
                    ref_table.lower(),
                    str(fk.get("referred_column") or "").lower(),
                )
            )
    return keys


def _relationship_key(row: SemanticRelationship) -> tuple[str, str, str, str] | None:
    try:
        fk = json.loads(row.source_fk_json or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(fk, dict):
        return None
    ref_table = str(fk.get("referred_table") or "")
    ref_schema = fk.get("referred_schema") or ""
    if "." not in ref_table and ref_schema:
        ref_table = f"{ref_schema}.{ref_table}"
    return (
        str(fk.get("from_table") or "").lower(),
        str(fk.get("column") or "").lower(),
        ref_table.lower(),
        str(fk.get("referred_column") or "").lower(),
    )


def _entity_name(qualified: str) -> str:
    schema_name, _, table = qualified.rpartition(".")
    bare = table.replace("_", " ").title()
    return f"{schema_name.replace('_', ' ').title()} {bare}" if schema_name else bare


# --- Preview ----------------------------------------------------------------------------


@dataclass
class KpiPreview:
    resolved: ResolvedKpi
    action: str  # create | update | link | unchanged | conflict | invalid
    existing_name: str | None = None
    existing_expression: str | None = None


@dataclass
class RelationshipPreview:
    resolved: ResolvedRelationship
    action: str  # create | confirm | unchanged | invalid
    note: str | None = None


def _connection_metrics(db: Session, tenant_id: UUID, connection_id: UUID) -> list[Metric]:
    return list(
        db.scalars(
            select(Metric).where(
                Metric.tenant_id == tenant_id, Metric.connection_id == connection_id
            )
        )
    )


def _connection_relationships(
    db: Session, tenant_id: UUID, connection_id: UUID
) -> dict[tuple[str, str, str, str], SemanticRelationship]:
    rows = db.scalars(
        select(SemanticRelationship).where(
            SemanticRelationship.tenant_id == tenant_id,
            SemanticRelationship.connection_id == connection_id,
        )
    )
    out: dict[tuple[str, str, str, str], SemanticRelationship] = {}
    for row in rows:
        key = _relationship_key(row)
        if key is not None:
            out.setdefault(key, row)
    return out


def _kpi_action(resolved: ResolvedKpi, metrics: list[Metric]) -> KpiPreview:
    if resolved.error:
        return KpiPreview(resolved, "invalid")
    by_name = next((m for m in metrics if m.name.lower() == resolved.kpi.name.lower()), None)
    if by_name is not None:
        if by_name.expression == resolved.expression:
            return KpiPreview(resolved, "unchanged", by_name.name, by_name.expression)
        if by_name.status == "confirmed":
            # A confirmed metric is in use; an import never rewrites it.
            return KpiPreview(resolved, "conflict", by_name.name, by_name.expression)
        return KpiPreview(resolved, "update", by_name.name, by_name.expression)
    if resolved.source_column_ref:
        same_column = next(
            (m for m in metrics if (m.source_column_ref or "").lower() == resolved.source_column_ref.lower()),
            None,
        )
        if same_column is not None:
            return KpiPreview(resolved, "link", same_column.name, same_column.expression)
    return KpiPreview(resolved, "create")


def preview_kpis(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    kpis: list[KpiRow],
    schema: dict[str, Any],
    validator: SqlValidator | None = None,
) -> list[KpiPreview]:
    metrics = _connection_metrics(db, tenant_id, connection_id)
    resolved_all = [resolve_kpi(kpi, schema) for kpi in kpis]
    grounded = [r for r in resolved_all if not r.error]
    if validator is not None and grounded:
        # Probe once: if the database can't be reached at all, report that
        # instead of marking every formula as rejected by it.
        try:
            validator(f"SELECT 1 AS ok FROM {grounded[0].table} WHERE 1 = 0")
        except Exception:  # noqa: BLE001
            validator = None
            for r in grounded:
                r.warnings.append("could not reach the database to test this formula")
    to_check = [r for r in resolved_all if not r.error]
    if validator is not None and to_check:
        # Each check is a network round trip to the customer database; run
        # a few at once rather than one after another.
        with ContextThreadPoolExecutor(max_workers=min(_VALIDATION_WORKERS, len(to_check))) as pool:
            list(pool.map(lambda r: validate_kpi_sql(r, validator), to_check))
    return [_kpi_action(r, metrics) for r in resolved_all]


def preview_relationships(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    relationships: list[RelationshipRow],
    schema: dict[str, Any],
) -> list[RelationshipPreview]:
    existing = _connection_relationships(db, tenant_id, connection_id)
    declared = _schema_fk_keys(schema)
    previews = []
    for rel in relationships:
        resolved = resolve_relationship(rel, schema)
        if resolved.error:
            previews.append(RelationshipPreview(resolved, "invalid"))
        elif resolved.key in declared:
            previews.append(
                RelationshipPreview(resolved, "unchanged", "already a foreign key in the database")
            )
        elif resolved.key in existing:
            row = existing[resolved.key]
            if row.status == "confirmed":
                previews.append(RelationshipPreview(resolved, "unchanged", "already saved"))
            else:
                previews.append(
                    RelationshipPreview(resolved, "confirm", "confirms a suggested relationship")
                )
        else:
            previews.append(RelationshipPreview(resolved, "create"))
    return previews


# --- Saving -----------------------------------------------------------------------------


def _metric_format(name: str, expression: str) -> str | None:
    lowered = name.lower()
    if "%" in name or "percent" in lowered or "* 100" in expression:
        return "percent"
    return None


def save_kpis(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    kpis: list[KpiRow],
    schema: dict[str, Any],
) -> dict[str, int]:
    """Re-resolve every KPI server-side and store it as a suggested metric.

    Does not commit. Invalid formulas are counted as rejected.
    """
    counts = {"created": 0, "updated": 0, "linked": 0, "unchanged": 0, "kept_existing": 0, "rejected": 0}
    metrics = _connection_metrics(db, tenant_id, connection_id)
    used_names, _dims, _entities = existing_semantic_names(db, tenant_id, connection_id)
    for kpi in kpis:
        resolved = resolve_kpi(kpi, schema)
        preview = _kpi_action(resolved, metrics)
        description = kpi.description or None
        domain = kpi.domain or None
        if preview.action == "invalid":
            counts["rejected"] += 1
        elif preview.action == "unchanged":
            counts["unchanged"] += 1
        elif preview.action == "conflict":
            counts["kept_existing"] += 1
        elif preview.action == "update":
            metric = next(m for m in metrics if m.name == preview.existing_name)
            metric.expression = resolved.expression
            metric.description = description or metric.description
            metric.domain = domain or metric.domain
            metric.format = _metric_format(kpi.name, resolved.expression) or metric.format
            metric.synonyms_json = encode_synonyms(
                *parse_synonyms(metric.synonyms_json), *kpi.synonyms, name=metric.name
            )
            _set_physical(metric, resolved)
            counts["updated"] += 1
        elif preview.action == "link":
            # Same column as an existing metric: teach it the KPI's name.
            metric = find_metric_by_source_ref(
                db, tenant_id, connection_id, resolved.source_column_ref or ""
            )
            if metric is None:
                counts["rejected"] += 1
                continue
            metric.synonyms_json = encode_synonyms(
                *parse_synonyms(metric.synonyms_json), kpi.name, *kpi.synonyms, name=metric.name
            )
            metric.description = metric.description or description
            metric.domain = metric.domain or domain
            counts["linked"] += 1
        else:
            name = unique_name(kpi.name, used_names)
            metric = Metric(
                tenant_id=tenant_id,
                connection_id=connection_id,
                name=name,
                expression=resolved.expression,
                label=kpi.name,
                description=description,
                format=_metric_format(kpi.name, resolved.expression),
                synonyms_json=encode_synonyms(kpi.name, *kpi.synonyms, name=name),
                status="suggested",
                domain=domain,
            )
            _set_physical(metric, resolved)
            db.add(metric)
            db.flush()
            metrics.append(metric)
            counts["created"] += 1
    return counts


def _set_physical(metric: Metric, resolved: ResolvedKpi) -> None:
    metric.source_column_ref = resolved.source_column_ref
    if resolved.source_column_ref:
        schema_name, table, column = physical_parts_from_ref(resolved.source_column_ref)
    else:
        schema_name, _, table = resolved.table.rpartition(".")
        column = None
    metric.physical_schema = schema_name or None
    metric.physical_table = table or None
    metric.physical_column = column


def save_relationships(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    relationships: list[RelationshipRow],
    schema: dict[str, Any],
) -> dict[str, int]:
    """Re-resolve every relationship server-side and store it as confirmed.

    Does not commit. Invalid rows are counted as rejected.
    """
    counts = {"created": 0, "confirmed": 0, "unchanged": 0, "rejected": 0}
    previews = preview_relationships(
        db, tenant_id=tenant_id, connection_id=connection_id,
        relationships=relationships, schema=schema,
    )
    existing = _connection_relationships(db, tenant_id, connection_id)
    for preview in previews:
        r = preview.resolved
        if preview.action == "invalid":
            counts["rejected"] += 1
        elif preview.action == "unchanged":
            counts["unchanged"] += 1
        elif preview.action == "confirm":
            existing[r.key].status = "confirmed"
            counts["confirmed"] += 1
        elif r.key in existing:  # duplicate row within this request
            counts["unchanged"] += 1
        else:
            row = SemanticRelationship(
                tenant_id=tenant_id,
                connection_id=connection_id,
                from_entity=_entity_name(r.from_table),
                to_entity=_entity_name(r.to_table),
                relationship_type=r.relationship_type,
                source_fk_json=r.fk_json(),
                status="confirmed",
            )
            db.add(row)
            existing[r.key] = row
            counts["created"] += 1
    return counts


# --- Feeding the agent ----------------------------------------------------------------------


def load_confirmed_relationships(
    db: Session, tenant_id: UUID, connection_id: UUID
) -> list[SemanticRelationship]:
    return list(
        db.scalars(
            select(SemanticRelationship).where(
                SemanticRelationship.tenant_id == tenant_id,
                SemanticRelationship.connection_id == connection_id,
                SemanticRelationship.status == "confirmed",
            )
        )
    )


def apply_relationships_to_schema(
    schema: dict[str, Any], relationships: Iterable[SemanticRelationship]
) -> dict[str, Any]:
    """Add confirmed relationships to the schema's foreign keys.

    Warehouses often declare no FK constraints, so without this the SQL
    prompt and join planner never see joins an admin has approved. Added
    entries carry ``source: "semantic"``; ones duplicating a declared FK
    are skipped.
    """
    index = _Schema(schema)
    by_qualified = {index.qualified(t).lower(): t for t in index.tables}
    declared = _schema_fk_keys(schema)
    for rel in relationships:
        key = _relationship_key(rel)
        if key is None or key in declared:
            continue
        table = by_qualified.get(key[0])
        target = by_qualified.get(key[2])
        if table is None or target is None:
            continue  # table no longer discovered
        from_col = _Schema.column(table, key[1])
        to_col = _Schema.column(target, key[3])
        if from_col is None or to_col is None:
            continue  # column no longer discovered
        table.setdefault("foreign_keys", []).append(
            {
                "column": str(from_col["name"]),
                "referred_schema": target.get("schema_name") or None,
                "referred_table": str(target["table_name"]),
                "referred_column": str(to_col["name"]),
                "source": "semantic",
            }
        )
        declared.add(key)
    return schema

