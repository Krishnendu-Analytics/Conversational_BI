"""Import a connection's own hand-authored KPI catalog into this app's
semantic layer.

Some customer databases include a self-documenting metadata table (we've
seen it named ``kpi_meta_data``) with columns like ``kpi_name``,
``formula``, ``domain`` — a business-authored KPI catalog sitting unused
next to the real data. This module reads it (via a read-only query against
the customer's own connection, same mechanism as any other query this app
runs) and creates ``Metric`` rows from the formulas it can resolve safely.

Scope is deliberately narrow, in two tiers:

Tier A — direct aggregate references, e.g. ``SUM(FACT_SALES[net_sales_value])``.
These map onto exactly one real column with no interpretation required, so
they're imported automatically.

Tier B — simple two-operand formulas (``A / B``, ``A - B``, ``A * B``,
``A + B``) where each side is a free-text business term (e.g. "Actual
Sales", "Sales Target") rather than a raw column reference. Resolution
tries a single table where BOTH operands match unambiguously first (the
common, low-risk case — e.g. "Actual Production / Planned Production" both
live in fact_production); only if no one table satisfies both does it fall
back to searching each operand independently across the whole connection
(the case a target-vs-actual comparison needs, since the target table is
usually separate). Either way, a match is accepted only when it is the
*unique* best-scoring column — a tie, or zero matches, is refused, not
guessed at: a business phrase like "Actual Sales" has no reliable way to
disambiguate against similarly-worded columns (e.g. net vs. gross sales)
purely from token overlap, and a wrongly-resolved operand becomes a
permanently stored, confirmed-looking metric that later questions would
silently trust — the same failure shape this whole session's other fixes
exist to prevent.

Formulas with more than two operands are out of scope entirely (skipped
and reported): a formula like "Net Sales - COGS - Freight" needs ordered
evaluation across three resolved quantities, not just a comparison.

Tier B never compiles the ratio itself as a single cross-table SQL
expression — that would require joining two fact tables on a shared
dimension key without pre-aggregating each side first, which fans out and
silently produces a wrong number. Instead, both operands are imported as
their own standalone metrics, tagged with the KPI's own ``domain``. The
existing domain-catalog curation (``app.agent.domain_catalog``) then
groups them under the same domain label even though they live in
different tables, so an investigation surfaces both sides next to each
other for a human (or a future comparison-aware synthesis step) to read
side by side, rather than one risky joined number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Metric
from app.query_execution import QueryCategory
from app.services.semantic_discovery import (
    existing_semantic_names,
    find_metric_by_source_ref,
    flush_new_semantic_row,
    physical_parts_from_ref,
)
from app.services.semantic_identity import encode_synonyms, humanize_identifier, unique_name
from app.tools.customer_db import get_stored_schema, run_readonly_query

_DIRECT_AGG_RE = re.compile(
    r"^\s*(SUM|AVG|COUNT|MIN|MAX)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]\s*\)\s*$",
    re.IGNORECASE,
)
# Exactly one operator, two operands — a formula with more than one operator
# (e.g. "Net Sales - COGS - Freight", "A / B * C") needs ordered evaluation
# across three resolved quantities, not a two-way comparison, and isn't
# attempted here.
_TWO_OPERAND_RE = re.compile(r"^\s*([^/\-*+]+?)\s*([/\-*+])\s*([^/\-*+]+?)\s*$")
_NUMERIC_TYPE_HINTS = ("int", "numeric", "decimal", "float", "double", "real", "money", "number")

# Words that qualify a business term without naming a distinct concept
# ("Actual Sales" means the sales figure, not a different metric from
# "Sales") — stripped before matching so they don't block an otherwise
# unambiguous match. Deliberately NOT stripping words like "target"/
# "planned"/"standard"/"budget": those select a genuinely different column
# family in this kind of schema (sales_target_mt vs net_sales_value) and
# must stay significant.
_QUALIFIER_STOPWORDS = {"actual", "current", "total", "the", "of", "a", "an", "value", "amount"}


@dataclass
class KpiImportSkip:
    kpi_name: str
    formula: str
    reason: str


@dataclass
class KpiImportResult:
    found: bool
    imported: list[str] = field(default_factory=list)
    # (kpi_name, operand_a_metric_name, operand_b_metric_name, operator)
    resolved_comparisons: list[tuple[str, str, str, str]] = field(default_factory=list)
    skipped: list[KpiImportSkip] = field(default_factory=list)


def _is_numeric_dtype(dtype: str) -> bool:
    lowered = (dtype or "").lower()
    return any(hint in lowered for hint in _NUMERIC_TYPE_HINTS)


def _is_key_column(name: str) -> bool:
    lowered = (name or "").lower()
    return lowered in {"id", "key", "pk"} or lowered.endswith(("_id", "_key", "_pk", "_uuid"))


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _QUALIFIER_STOPWORDS}


def _find_table(schema: dict[str, Any], table_hint: str) -> dict[str, Any] | None:
    """Resolve a formula's bare TABLE reference (e.g. "FACT_SALES") to the
    real discovered table dict, case-insensitively."""
    target = table_hint.strip().lower()
    for table in schema.get("tables") or []:
        if isinstance(table, dict) and str(table.get("table_name") or "").lower() == target:
            return table
    return None


def _numeric_columns(table: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        col
        for col in (table.get("columns") or [])
        if isinstance(col, dict)
        and col.get("name")
        and not _is_key_column(str(col["name"]))
        and _is_numeric_dtype(str(col.get("data_type") or ""))
    ]


def _qualified_name(table: dict[str, Any]) -> str:
    table_name = str(table.get("table_name") or "")
    schema_name = table.get("schema_name") or ""
    return f"{schema_name}.{table_name}" if schema_name else table_name


@dataclass
class _Candidate:
    qualified_table: str
    column: str
    label: str = ""


def _scored_candidates(tokens: set[str], tables: list[dict[str, Any]]) -> list[tuple[int, _Candidate]]:
    """Every numeric column across ``tables`` whose humanized name is a
    token-subset match with ``tokens`` — every candidate individually, with
    its own score, never pre-collapsed per table. Collapsing per table
    before comparing globally is a real bug this function exists to avoid:
    two candidates tying *within one table* must still compete on equal
    footing against a candidate in a different table, not silently vanish
    and hand the match to whatever's left by default.
    """
    scored: list[tuple[int, _Candidate]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        qualified = _qualified_name(table)
        for col in _numeric_columns(table):
            col_tokens = _tokens(humanize_identifier(str(col["name"])))
            if not col_tokens:
                continue
            if tokens <= col_tokens or col_tokens <= tokens:
                score = len(tokens & col_tokens)
                scored.append((score, _Candidate(qualified_table=qualified, column=str(col["name"]))))
    return scored


def _best_unique(scored: list[tuple[int, _Candidate]]) -> _Candidate | None:
    if not scored:
        return None
    ranked = sorted(scored, key=lambda x: x[0], reverse=True)
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _match_within_table(tokens: set[str], table: dict[str, Any]) -> _Candidate | None:
    return _best_unique(_scored_candidates(tokens, [table]))


def _match_across_tables(tokens: set[str], schema: dict[str, Any]) -> _Candidate | None:
    return _best_unique(_scored_candidates(tokens, list(schema.get("tables") or [])))


def _resolve_pair_within_table(
    left_tokens: set[str], right_tokens: set[str], table: dict[str, Any]
) -> tuple[_Candidate | None, _Candidate | None]:
    """Jointly resolve both operands within ONE table. Each side alone may
    tie (e.g. "Actual Production" ties between actual_production_mt and
    planned_production_mt — both merely contain "production"), but once the
    OTHER side resolves unambiguously (e.g. "Planned Production" only
    matches planned_production_mt, thanks to "planned" being a real,
    unstripped token), excluding that column from the tied side's
    candidates often breaks the tie correctly — the same reasoning a human
    skimming the column list would use.
    """
    left_scored = _scored_candidates(left_tokens, [table])
    right_scored = _scored_candidates(right_tokens, [table])
    left_best = _best_unique(left_scored)
    right_best = _best_unique(right_scored)

    if left_best is not None and right_best is not None and left_best.column != right_best.column:
        return left_best, right_best

    if right_best is not None:
        narrowed = [(s, c) for s, c in left_scored if c.column != right_best.column]
        resolved = _best_unique(narrowed)
        if resolved is not None:
            return resolved, right_best
    if left_best is not None:
        narrowed = [(s, c) for s, c in right_scored if c.column != left_best.column]
        resolved = _best_unique(narrowed)
        if resolved is not None:
            return left_best, resolved
    return None, None


def _resolve_operand(
    text: str, *, schema: dict[str, Any], resolved_by_name: dict[str, _Candidate]
) -> _Candidate | None:
    tokens = _tokens(text)
    if not tokens:
        return None
    named = next((c for name, c in resolved_by_name.items() if _tokens(name) == tokens), None)
    if named is not None:
        return named
    return _match_across_tables(tokens, schema)


def _resolve_pair(
    left_text: str,
    right_text: str,
    *,
    schema: dict[str, Any],
    resolved_by_name: dict[str, _Candidate],
) -> tuple[_Candidate | None, _Candidate | None]:
    """Resolve as much of a two-operand formula as can be done safely.

    Either side may come back None — a formula where only one operand is
    resolvable (e.g. "Sales Target" is unambiguous even when "Actual Sales"
    alone isn't, because "sales" alone can't tell gross from net from
    target) still yields one real, useful metric rather than discarding
    both just because the pair as a whole couldn't be completed.
    """
    left_tokens = _tokens(left_text)
    right_tokens = _tokens(right_text)
    if not left_tokens or not right_tokens:
        return None, None

    left_named = next(
        (c for name, c in resolved_by_name.items() if _tokens(name) == left_tokens), None
    )
    right_named = next(
        (c for name, c in resolved_by_name.items() if _tokens(name) == right_tokens), None
    )

    if left_named is None and right_named is None:
        # Prefer a single table where BOTH operands resolve unambiguously —
        # the common, low-risk case — before ever falling back to
        # independent cross-table resolution for either side.
        for table in schema.get("tables") or []:
            if not isinstance(table, dict):
                continue
            left_in_table, right_in_table = _resolve_pair_within_table(left_tokens, right_tokens, table)
            if left_in_table is not None and right_in_table is not None:
                left_in_table.label = left_text.strip()
                right_in_table.label = right_text.strip()
                return left_in_table, right_in_table

    left = left_named or _resolve_operand(left_text, schema=schema, resolved_by_name=resolved_by_name)
    right = right_named or _resolve_operand(right_text, schema=schema, resolved_by_name=resolved_by_name)
    if left is not None and not left.label:
        left.label = left_text.strip()
    if right is not None and not right.label:
        right.label = right_text.strip()
    return left, right


def _upsert_direct_metric(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    name: str,
    domain: str | None,
    agg: str,
    qualified_table: str,
    column: str,
    used_names: set[str],
) -> Metric | None:
    col_ref = f"{qualified_table}.{column}"
    existing = find_metric_by_source_ref(db, tenant_id, connection_id, col_ref)
    if existing is not None:
        # A prior schema-only heuristic pass (app.services.semantic_discovery)
        # may already have created this metric with no domain — the KPI
        # catalog's own domain is strictly better information than that
        # generic guess, so backfill it. Never overwrites an already-set
        # domain (e.g. from a human's own edit or an earlier KPI import).
        if not existing.domain and domain:
            existing.domain = domain
        return existing

    unique = unique_name(name, used_names)
    phys_schema, phys_table, phys_column = physical_parts_from_ref(col_ref)
    metric = Metric(
        tenant_id=tenant_id,
        connection_id=connection_id,
        name=unique,
        expression=f"{agg.upper()}({col_ref})",
        label=name,
        domain=domain,
        synonyms_json=encode_synonyms(name, name=unique),
        status="suggested",
        source_column_ref=col_ref,
        physical_schema=phys_schema,
        physical_table=phys_table,
        physical_column=phys_column,
    )
    if flush_new_semantic_row(db, metric):
        used_names.add(unique.lower())
        return metric
    return find_metric_by_source_ref(db, tenant_id, connection_id, col_ref)


def import_kpi_catalog(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    table_name: str = "kpi_meta_data",
    schema_name: str | None = None,
) -> KpiImportResult:
    """Read ``table_name`` from the connection's own database and create
    ``suggested`` Metric rows for whatever formulas can be safely resolved.

    Always imports as ``status="suggested"`` — the same review workflow
    every other semantic suggestion in this app goes through
    (``app.services.semantic_discovery``) before it's trusted by query
    generation. Nothing here is auto-confirmed.
    """
    schema = get_stored_schema(db, connection_id, tenant_id=tenant_id)
    if not schema or not schema.get("tables"):
        return KpiImportResult(found=False)

    qualified_kpi_table = f"{schema_name}.{table_name}" if schema_name else table_name
    try:
        rows = run_readonly_query(
            str(connection_id),
            f"SELECT * FROM {qualified_kpi_table}",
            tenant_id=tenant_id,
            category=QueryCategory.ANALYTICS,
        )
    except Exception:  # noqa: BLE001
        # No such table (or it's not readable) — a connection without this
        # pattern is the common case, not an error.
        return KpiImportResult(found=False)

    result = KpiImportResult(found=True)
    metric_names, _dim_names, _entity_names = existing_semantic_names(db, tenant_id, connection_id)
    resolved_by_name: dict[str, _Candidate] = {}

    for row in rows:
        kpi_name = str(row.get("kpi_name") or row.get("name") or "").strip()
        formula = str(row.get("formula") or "").strip()
        raw_domain = row.get("domain")
        domain = str(raw_domain).strip() if raw_domain else None
        if not kpi_name or not formula:
            continue

        direct = _DIRECT_AGG_RE.match(formula)
        if direct:
            agg, table_hint, column_hint = direct.groups()
            table = _find_table(schema, table_hint)
            if table is None:
                result.skipped.append(
                    KpiImportSkip(kpi_name, formula, f"Table {table_hint} not found in discovered schema.")
                )
                continue
            column = next(
                (
                    c["name"]
                    for c in (table.get("columns") or [])
                    if isinstance(c, dict) and str(c.get("name", "")).lower() == column_hint.lower()
                ),
                None,
            )
            if column is None:
                result.skipped.append(
                    KpiImportSkip(kpi_name, formula, f"Column {column_hint} not found on {table_hint}.")
                )
                continue
            qualified = _qualified_name(table)
            metric = _upsert_direct_metric(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                name=kpi_name,
                domain=domain,
                agg=agg,
                qualified_table=qualified,
                column=column,
                used_names=metric_names,
            )
            if metric is not None:
                if metric.name not in result.imported:
                    result.imported.append(metric.name)
                resolved_by_name[kpi_name] = _Candidate(qualified_table=qualified, column=column, label=kpi_name)
            else:
                result.skipped.append(KpiImportSkip(kpi_name, formula, "Could not create metric row."))
            continue

        two_op = _TWO_OPERAND_RE.match(formula)
        if not two_op:
            result.skipped.append(
                KpiImportSkip(kpi_name, formula, "Formula shape not supported (needs manual review).")
            )
            continue

        left_text, operator, right_text = two_op.groups()
        left, right = _resolve_pair(left_text, right_text, schema=schema, resolved_by_name=resolved_by_name)

        # Import whichever side resolved, even if the other didn't — "Sales
        # Target" is a perfectly good standalone metric on its own merits,
        # regardless of whether "Actual Sales" could also be disambiguated.
        # Requiring the whole pair to succeed would throw away a real,
        # useful, unambiguous metric just because its formula-mate wasn't
        # resolvable — see module docstring.
        left_metric = None
        if left is not None:
            left_metric = _upsert_direct_metric(
                db, tenant_id=tenant_id, connection_id=connection_id, name=left.label, domain=domain,
                agg="SUM", qualified_table=left.qualified_table, column=left.column, used_names=metric_names,
            )
            if left_metric is not None:
                resolved_by_name.setdefault(
                    left_metric.label or left_metric.name,
                    _Candidate(qualified_table=left.qualified_table, column=left.column, label=left_metric.label or left_metric.name),
                )
                if left_metric.name not in result.imported:
                    result.imported.append(left_metric.name)

        right_metric = None
        if right is not None:
            right_metric = _upsert_direct_metric(
                db, tenant_id=tenant_id, connection_id=connection_id, name=right.label, domain=domain,
                agg="SUM", qualified_table=right.qualified_table, column=right.column, used_names=metric_names,
            )
            if right_metric is not None:
                resolved_by_name.setdefault(
                    right_metric.label or right_metric.name,
                    _Candidate(qualified_table=right.qualified_table, column=right.column, label=right_metric.label or right_metric.name),
                )
                if right_metric.name not in result.imported:
                    result.imported.append(right_metric.name)

        if left_metric is not None and right_metric is not None:
            result.resolved_comparisons.append((kpi_name, left_metric.name, right_metric.name, operator))
        else:
            unresolved = []
            if left_metric is None:
                unresolved.append(f'"{left_text.strip()}"')
            if right_metric is None:
                unresolved.append(f'"{right_text.strip()}"')
            note = " (other operand imported on its own)" if (left_metric or right_metric) else ""
            result.skipped.append(
                KpiImportSkip(
                    kpi_name,
                    formula,
                    f"Could not uniquely resolve {' and '.join(unresolved)} to a single column.{note}",
                )
            )

    if result.imported:
        db.commit()
    return result
