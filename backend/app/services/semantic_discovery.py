"""Heuristic + optional LLM semantic inference from discovered schema."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.usage import ContextThreadPoolExecutor
from app.services.data_dictionary import load_entries
from app.models import (
    Dataset,
    Dimension,
    Metric,
    SchemaTable,
    SemanticEntity,
    SemanticRelationship,
)
from app.redis_client import semantic_cache
from app.schemas import SchemaTablePayload
from app.services.semantic_identity import (
    encode_synonyms,
    field_identity_name,
    unique_name,
)
from app.services.tabular_headers import (
    expression_references_schema_column,
    is_placeholder_column_name,
)
from app.tools.sql_identifiers import parse_qualified_ref

_NUMERIC_TYPES = ("int", "numeric", "decimal", "float", "double", "real", "money", "number")
_DATE_TYPES = ("date", "time", "timestamp")

# Heuristic suggestions stay domain-neutral: every measure keeps its own
# column name as its label (the LLM pass and the admin add business names).
# Generic category labels used to leak into names *and* synonyms, so any
# column with total/value/balance/cost became "Revenue" — a hospital's
# total_beds, a bank's account_balance — and columns outside that
# vocabulary (length_of_stay_days, readmissions) were never suggested.
#
# Money is the one unit worth guessing from a name, and only from words
# that mean money in every industry.
_MONEY_COLUMN = re.compile(
    r"(^|_)(amt|amount|amounts|revenue|revenues|price|prices|cost|costs|fee|fees|"
    r"charge|charges|paid|payment|payments|salary|salaries|income|expense|expenses|spend)(_|$)"
)
# Numeric calendar parts (year, month_number, day_of_week...) are periods,
# not measures. "days" (a duration) is a measure and does not match.
_DATE_PART_COLUMN = re.compile(r"(^|_)(year|month|quarter|week|day|hour|yyyymm|yyyymmdd)(_|$)")
# Free text: labelling or grouping by it is never useful.
_FREE_TEXT_COLUMN = re.compile(
    r"(^|_)(description|desc|comment|comments|note|notes|remark|remarks|address|"
    r"email|phone|mobile|url|uri|password|hash|token|json|xml|body|message)(_|$)"
)
_TEXT_TYPES = ("char", "text", "string", "varchar", "nvarchar", "clob")

# Numeric columns that identify rows rather than measure anything: summing
# them (e.g. SUM(sales_key)) is meaningless.
_IDENTIFIER_COLUMN = re.compile(r"(^id$|(^|_)(id|key|sk|fk|pk)$)")
_IDENTIFIER_DESCRIPTION = re.compile(
    r"\b(surrogate key|foreign key|primary key|unique (internal )?(id|identifier)|"
    r"identifier (assigned|used) to)\b"
)
# Per-unit / ratio measures: averaging is meaningful, summing is not.
_RATE_COLUMN = re.compile(r"(^|_)(per|pct|percent|percentage|ratio|rate|price|avg|average)(_|$)")
_RATE_DESCRIPTION = re.compile(r"\b(per [a-z]+|percentage|average|ratio)\b")
# Self-documentation tables (e.g. kpi_meta_data) describe the data, they
# aren't business data themselves.
_METADATA_TABLE = re.compile(r"(^|_)(meta_?data|metadata)(_|$)")
# An explicit dimension-table name. A bare "d_" prefix is not a convention
# anyone can rely on (a hospital's d_charges is a fact table).
_DIMENSION_TABLE = re.compile(r"^dim_")


def _is_identifier_column(col_name: str, description: str) -> bool:
    return bool(
        _IDENTIFIER_COLUMN.search(col_name.lower())
        or _IDENTIFIER_DESCRIPTION.search(description.lower())
    )


def _aggregation_for(
    col_name: str,
    table_name: str,
    description: str,
    default: str,
    *,
    lookup_table: bool = False,
) -> str:
    """AVG for rates/prices and for numeric attributes of lookup tables (a
    product's standard cost summed across products means nothing). A lookup
    table is named dim_* or, structurally, is referenced by other tables'
    foreign keys without referencing any itself."""
    if (
        _RATE_COLUMN.search(col_name.lower())
        or _RATE_DESCRIPTION.search(description.lower())
        or _DIMENSION_TABLE.search(table_name.lower())
        or lookup_table
    ):
        return "AVG({expr})"
    return default


def _humanize(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip().title() or name


def _lookup_tables(tables: list[SchemaTable]) -> set[str]:
    """Bare table names (lower-case) that other tables point at and that
    point at nothing themselves: lookup/dimension tables by structure."""
    referenced: set[str] = set()
    referencing: set[str] = set()
    for row in tables:
        try:
            payload = SchemaTablePayload.model_validate_json(row.columns_json)
        except Exception:  # noqa: BLE001
            continue
        bare = row.table_name.split(".")[-1].lower()
        for fk in payload.foreign_keys:
            target = (fk.referred_table or "").split(".")[-1].lower()
            if target and target != bare:
                referenced.add(target)
                referencing.add(bare)
    return referenced - referencing


def _is_numeric(dtype: str) -> bool:
    lowered = (dtype or "").lower()
    return any(token in lowered for token in _NUMERIC_TYPES)


def _is_date(dtype: str) -> bool:
    lowered = (dtype or "").lower()
    return any(token in lowered for token in _DATE_TYPES)


def _qualified_table(schema_name: str | None, table_name: str) -> str:
    bare = table_name.split(".")[-1]
    if schema_name:
        return f"{schema_name}.{bare}"
    return table_name


def _entity_name(schema_name: str | None, table_name: str) -> str:
    bare = table_name.split(".")[-1].replace("_", " ").title()
    if schema_name:
        schema_title = schema_name.replace("_", " ").title()
        return f"{schema_title} {bare}"
    return bare


def _physical_parts(col_ref: str) -> tuple[str | None, str | None, str | None]:
    if "." not in col_ref:
        return None, None, col_ref
    table_part, col = col_ref.rsplit(".", 1)
    schema, table = parse_qualified_ref(table_part)
    return schema, table, col


def _metric_name_for_column(col_name: str, dtype: str = "") -> tuple[str, str, str] | None:
    """(label, aggregation template, format) for a numeric, non-identifier
    column; None for calendar parts. The label is the column's own name."""
    lowered = col_name.lower()
    if _DATE_PART_COLUMN.search(lowered):
        return None
    money = "money" in (dtype or "").lower() or bool(_MONEY_COLUMN.search(lowered))
    return _humanize(col_name), "SUM({expr})", "currency" if money else "number"


def _dimension_generic_label(col_name: str, dtype: str) -> str | None:
    """Label for a column worth grouping by (its own readable name), or None:
    dates, and text columns that are not free text."""
    lowered = col_name.lower()
    if _is_date(dtype):
        return "Date"  # by type, not by industry: "by date" should find it
    if any(t in (dtype or "").lower() for t in _TEXT_TYPES) and not _FREE_TEXT_COLUMN.search(lowered):
        return _humanize(col_name)
    return None


def _unique_name(base: str, used: set[str]) -> str:
    return unique_name(base, used)


def _table_display_label(
    db: Session,
    tenant_id: UUID,
    connection_id: UUID,
    schema_name: str | None,
    table_name: str,
) -> str:
    bare = table_name.split(".")[-1]
    dataset = db.scalar(
        select(Dataset).where(
            Dataset.tenant_id == tenant_id,
            Dataset.connection_id == connection_id,
            Dataset.physical_table == bare,
        )
    )
    if dataset is not None:
        return (dataset.display_name or dataset.name or _entity_name(schema_name, table_name)).strip()
    return _entity_name(schema_name, table_name)


def _flush_new(db: Session, obj: object) -> bool:
    """Insert with a savepoint so unique conflicts do not abort the batch."""
    try:
        with db.begin_nested():
            db.add(obj)
            db.flush()
        return True
    except IntegrityError:
        return False


def _existing_names(
    db: Session, tenant_id: UUID, connection_id: UUID
) -> tuple[set[str], set[str], set[str]]:
    metrics = {
        row.name.lower()
        for row in db.scalars(
            select(Metric).where(
                Metric.tenant_id == tenant_id,
                Metric.connection_id == connection_id,
            )
        ).all()
    }
    dimensions = {
        row.name.lower()
        for row in db.scalars(
            select(Dimension).where(
                Dimension.tenant_id == tenant_id,
                Dimension.connection_id == connection_id,
            )
        ).all()
    }
    entities = {
        row.name.lower()
        for row in db.scalars(
            select(SemanticEntity).where(
                SemanticEntity.tenant_id == tenant_id,
                SemanticEntity.connection_id == connection_id,
            )
        ).all()
    }
    return metrics, dimensions, entities


def _find_metric_by_ref(
    db: Session, tenant_id: UUID, connection_id: UUID, col_ref: str
) -> Metric | None:
    return db.scalar(
        select(Metric).where(
            Metric.tenant_id == tenant_id,
            Metric.connection_id == connection_id,
            Metric.source_column_ref == col_ref,
        )
    )


def _find_dimension_by_ref(
    db: Session, tenant_id: UUID, connection_id: UUID, col_ref: str
) -> Dimension | None:
    return db.scalar(
        select(Dimension).where(
            Dimension.tenant_id == tenant_id,
            Dimension.connection_id == connection_id,
            Dimension.source_column_ref == col_ref,
        )
    )


def _find_entity_by_source(
    db: Session, tenant_id: UUID, connection_id: UUID, source_table: str
) -> SemanticEntity | None:
    return db.scalar(
        select(SemanticEntity).where(
            SemanticEntity.tenant_id == tenant_id,
            SemanticEntity.connection_id == connection_id,
            SemanticEntity.source_table == source_table,
        )
    )


def _find_metric_by_expression(
    db: Session, tenant_id: UUID, connection_id: UUID, expression: str
) -> Metric | None:
    return db.scalar(
        select(Metric).where(
            Metric.tenant_id == tenant_id,
            Metric.connection_id == connection_id,
            Metric.expression == expression,
        )
    )


def _find_dimension_by_expression(
    db: Session, tenant_id: UUID, connection_id: UUID, expression: str
) -> Dimension | None:
    return db.scalar(
        select(Dimension).where(
            Dimension.tenant_id == tenant_id,
            Dimension.connection_id == connection_id,
            Dimension.expression == expression,
        )
    )


def _heuristic_suggestions(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    tables: list[SchemaTable],
) -> dict[str, int]:
    metric_names, dimension_names, entity_names = _existing_names(
        db, tenant_id, connection_id
    )
    counts = {"metrics": 0, "dimensions": 0, "entities": 0, "relationships": 0}
    seen_refs: set[str] = set()
    column_descriptions = {
        (e.table_name.lower(), e.column_name.lower()): e.description
        for e in load_entries(db, tenant_id, connection_id)
        if e.column_name
    }
    lookup_tables = _lookup_tables(tables)

    for row in tables:
        payload = SchemaTablePayload.model_validate_json(row.columns_json)
        schema_name = row.schema_name or payload.schema_name
        qualified = _qualified_table(schema_name, row.table_name)
        entity_label = _entity_name(schema_name, row.table_name)
        phys_schema, phys_table, _ = _physical_parts(qualified)

        display_label = _table_display_label(
            db, tenant_id, connection_id, schema_name, row.table_name
        )
        existing_entity = _find_entity_by_source(db, tenant_id, connection_id, qualified)
        if existing_entity is None:
            entity_name = _unique_name(entity_label, entity_names)
            entity = SemanticEntity(
                tenant_id=tenant_id,
                connection_id=connection_id,
                name=entity_name,
                description=f"Inferred from table {qualified}",
                source_table=qualified,
                physical_schema=phys_schema,
                physical_table=phys_table or row.table_name,
                status="suggested",
            )
            if _flush_new(db, entity):
                counts["entities"] += 1
            else:
                recovered = _find_entity_by_source(db, tenant_id, connection_id, qualified)
                if recovered is None:
                    entity_name = _unique_name(entity_name, entity_names)
                    retry = SemanticEntity(
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        name=entity_name,
                        description=f"Inferred from table {qualified}",
                        source_table=qualified,
                        physical_schema=phys_schema,
                        physical_table=phys_table or row.table_name,
                        status="suggested",
                    )
                    if _flush_new(db, retry):
                        counts["entities"] += 1
        elif existing_entity is not None and existing_entity.status == "confirmed":
            existing_entity.physical_schema = phys_schema
            existing_entity.physical_table = phys_table or row.table_name
            existing_entity.source_table = qualified

        if _METADATA_TABLE.search(row.table_name.lower()):
            continue

        for col in payload.columns:
            if col.is_pk:
                continue
            description = column_descriptions.get(
                (row.table_name.lower(), col.name.lower()), ""
            )
            col_ref = f"{qualified}.{col.name}"
            seen_refs.add(col_ref)
            p_schema, p_table, p_col = _physical_parts(col_ref)

            existing_metric = _find_metric_by_ref(db, tenant_id, connection_id, col_ref)
            if existing_metric is not None:
                if existing_metric.status == "confirmed":
                    existing_metric.physical_schema = p_schema
                    existing_metric.physical_table = p_table
                    existing_metric.physical_column = p_col
                    existing_metric.source_column_ref = col_ref
                continue

            if is_placeholder_column_name(col.name):
                continue
            if _is_numeric(col.data_type):
                if _is_identifier_column(col.name, description):
                    continue
                metric_hint = _metric_name_for_column(col.name, col.data_type)
                if metric_hint is None:
                    continue
                label, agg_tpl, fmt = metric_hint
                agg_tpl = _aggregation_for(
                    col.name,
                    row.table_name,
                    description,
                    agg_tpl,
                    lookup_table=row.table_name.split(".")[-1].lower() in lookup_tables,
                )
                if db.scalar(
                    select(Metric).where(
                        Metric.tenant_id == tenant_id,
                        Metric.connection_id == connection_id,
                        Metric.source_column_ref == col_ref,
                    )
                ):
                    continue
                name = field_identity_name(
                    col.name, table_label=display_label, used=metric_names
                )
                metric = Metric(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    name=name,
                    expression=agg_tpl.format(expr=col_ref),
                    label=label,
                    format=fmt,
                    synonyms_json=encode_synonyms(label, name=name),
                    status="suggested",
                    source_column_ref=col_ref,
                    physical_schema=p_schema,
                    physical_table=p_table,
                    physical_column=p_col,
                )
                if _flush_new(db, metric):
                    counts["metrics"] += 1
                else:
                    recovered = _find_metric_by_ref(db, tenant_id, connection_id, col_ref)
                    if recovered is None:
                        name = _unique_name(name, metric_names)
                        retry = Metric(
                            tenant_id=tenant_id,
                            connection_id=connection_id,
                            name=name,
                            expression=agg_tpl.format(expr=col_ref),
                            label=label,
                            format=fmt,
                            synonyms_json=encode_synonyms(label, name=name),
                            status="suggested",
                            source_column_ref=col_ref,
                            physical_schema=p_schema,
                            physical_table=p_table,
                            physical_column=p_col,
                        )
                        if _flush_new(db, retry):
                            counts["metrics"] += 1
                continue

            existing_dim = _find_dimension_by_ref(db, tenant_id, connection_id, col_ref)
            if existing_dim is not None:
                if existing_dim.status == "confirmed":
                    existing_dim.physical_schema = p_schema
                    existing_dim.physical_table = p_table
                    existing_dim.physical_column = p_col
                    existing_dim.source_column_ref = col_ref
                continue

            if is_placeholder_column_name(col.name):
                continue
            dim_label = _dimension_generic_label(col.name, col.data_type)
            if dim_label is None:
                continue
            name = field_identity_name(
                col.name, table_label=display_label, used=dimension_names
            )
            dimension = Dimension(
                tenant_id=tenant_id,
                connection_id=connection_id,
                name=name,
                expression=col_ref,
                label=dim_label,
                format="date" if _is_date(col.data_type) else None,
                synonyms_json=encode_synonyms(dim_label, name=name),
                status="suggested",
                source_column_ref=col_ref,
                physical_schema=p_schema,
                physical_table=p_table,
                physical_column=p_col,
            )
            if _flush_new(db, dimension):
                counts["dimensions"] += 1
            else:
                recovered = _find_dimension_by_ref(db, tenant_id, connection_id, col_ref)
                if recovered is None:
                    name = _unique_name(name, dimension_names)
                    retry = Dimension(
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        name=name,
                        expression=col_ref,
                        label=dim_label,
                        format="date" if _is_date(col.data_type) else None,
                        synonyms_json=encode_synonyms(dim_label, name=name),
                        status="suggested",
                        source_column_ref=col_ref,
                        physical_schema=p_schema,
                        physical_table=p_table,
                        physical_column=p_col,
                    )
                    if _flush_new(db, retry):
                        counts["dimensions"] += 1

        for fk in payload.foreign_keys:
            from_entity = entity_label
            to_entity = _entity_name(
                fk.referred_schema,
                fk.referred_table.split(".")[-1] if fk.referred_table else "",
            )
            existing = db.scalar(
                select(SemanticRelationship).where(
                    SemanticRelationship.tenant_id == tenant_id,
                    SemanticRelationship.connection_id == connection_id,
                    SemanticRelationship.from_entity == from_entity,
                    SemanticRelationship.to_entity == to_entity,
                )
            )
            if existing is not None:
                if existing.status != "confirmed":
                    existing.source_fk_json = json.dumps(
                        {
                            "column": fk.column,
                            "referred_schema": fk.referred_schema,
                            "referred_table": fk.referred_table,
                            "referred_column": fk.referred_column,
                            "from_table": qualified,
                        }
                    )
                continue
            relationship = SemanticRelationship(
                tenant_id=tenant_id,
                connection_id=connection_id,
                from_entity=from_entity,
                to_entity=to_entity,
                relationship_type="many_to_one",
                source_fk_json=json.dumps(
                    {
                        "column": fk.column,
                        "referred_schema": getattr(fk, "referred_schema", None),
                        "referred_table": fk.referred_table,
                        "referred_column": fk.referred_column,
                        "from_table": qualified,
                    }
                ),
                status="suggested",
            )
            if _flush_new(db, relationship):
                counts["relationships"] += 1

    # Deprecate suggested rows whose physical refs disappeared (never touch confirmed).
    for metric in db.scalars(
        select(Metric).where(
            Metric.tenant_id == tenant_id,
            Metric.connection_id == connection_id,
            Metric.status == "suggested",
        )
    ).all():
        if metric.source_column_ref and metric.source_column_ref not in seen_refs:
            metric.status = "deprecated"

    for dimension in db.scalars(
        select(Dimension).where(
            Dimension.tenant_id == tenant_id,
            Dimension.connection_id == connection_id,
            Dimension.status == "suggested",
        )
    ).all():
        if dimension.source_column_ref and dimension.source_column_ref not in seen_refs:
            dimension.status = "deprecated"

    return counts


def _schema_column_catalog(tables: list[SchemaTable]) -> tuple[set[str], set[str]]:
    names: set[str] = set()
    refs: set[str] = set()
    for row in tables:
        try:
            payload = SchemaTablePayload.model_validate_json(row.columns_json)
        except Exception:  # noqa: BLE001
            continue
        schema_name = row.schema_name or payload.schema_name
        qualified = _qualified_table(schema_name, row.table_name)
        for col in payload.columns:
            if is_placeholder_column_name(col.name):
                continue
            names.add(col.name)
            refs.add(f"{qualified}.{col.name}")
            refs.add(col.name)
    return names, refs


# --- Public re-exports for other services -------------------------------
# app.services.kpi_catalog_import (importing a connection's own
# hand-authored KPI catalog into the semantic layer) reuses these
# dedup/grounding building blocks rather than duplicating them, so a fix
# here — e.g. to unique-name collision handling — applies everywhere.
flush_new_semantic_row = _flush_new
qualified_table_name = _qualified_table
physical_parts_from_ref = _physical_parts
find_metric_by_source_ref = _find_metric_by_ref
find_metric_by_expression = _find_metric_by_expression
is_numeric_column_type = _is_numeric
existing_semantic_names = _existing_names


def discover_semantic_suggestions(
    db: Session,
    connection_id: UUID,
    tenant_id: UUID,
    *,
    llm_client: LLMClient | None = None,
    use_llm: bool = True,
) -> dict[str, int]:
    """Infer semantic layer rows from cached schema_tables for a connection."""
    tables = list(
        db.scalars(
            select(SchemaTable)
            .where(
                SchemaTable.connection_id == connection_id,
                SchemaTable.tenant_id == tenant_id,
            )
            .order_by(SchemaTable.schema_name, SchemaTable.table_name)
        ).all()
    )
    if not tables:
        return {"metrics": 0, "dimensions": 0, "entities": 0, "relationships": 0}

    counts = _heuristic_suggestions(
        db, tenant_id=tenant_id, connection_id=connection_id, tables=tables
    )

    settings = get_settings()
    client = llm_client
    if use_llm and client is None and settings.llm_api_key:
        client = LLMClient()

    if use_llm and client is not None and client.client is not None:
        batches = _llm_table_batches(tables)
        # Batches run in parallel; results are applied one after another in
        # this session so a later batch skips what an earlier one added.
        with ContextThreadPoolExecutor(max_workers=min(_LLM_WORKERS, len(batches) or 1)) as pool:
            results = list(pool.map(lambda lines: _llm_suggest(client, lines), batches))
        column_names, column_refs = _schema_column_catalog(tables)
        for parsed in results:
            if not parsed:
                continue
            llm_counts = _apply_llm_suggestions(
                db,
                tenant_id=tenant_id,
                connection_id=connection_id,
                suggestions=parsed,
                column_names=column_names,
                column_refs=column_refs,
            )
            for key, value in llm_counts.items():
                counts[key] = counts.get(key, 0) + value

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        counts = _heuristic_suggestions(
            db, tenant_id=tenant_id, connection_id=connection_id, tables=tables
        )
        db.commit()
    semantic_cache.invalidate(tenant_id, connection_id)
    return counts


# Every table goes to the LLM, in batches small enough for a reliable JSON
# answer; each batch is one call. Beyond _LLM_MAX_TABLES only the heuristic
# pass runs, which keeps one click from costing an unbounded number of calls.
_LLM_TABLES_PER_BATCH = 15
_LLM_COLUMNS_PER_TABLE = 60
_LLM_MAX_TABLES = 300
_LLM_WORKERS = 4


def _llm_table_batches(tables: list[SchemaTable]) -> list[list[str]]:
    lines: list[str] = []
    for row in tables[:_LLM_MAX_TABLES]:
        payload = SchemaTablePayload.model_validate_json(row.columns_json)
        schema_name = row.schema_name or payload.schema_name or ""
        usable = [c for c in payload.columns if not is_placeholder_column_name(c.name)]
        cols = ", ".join(f"{c.name} {c.data_type}" for c in usable[:_LLM_COLUMNS_PER_TABLE])
        lines.append(f"{schema_name}.{row.table_name}({cols})")
    return [
        lines[i : i + _LLM_TABLES_PER_BATCH]
        for i in range(0, len(lines), _LLM_TABLES_PER_BATCH)
    ]


def _llm_suggest(client: LLMClient, schema_lines: list[str]) -> dict[str, Any] | None:
    prompt = (
        "Infer business metrics, dimensions, entities, and relationships from this schema. "
        "Return ONLY JSON with keys metrics, dimensions, entities, relationships. "
        "Each metric needs name and expression; each dimension needs name and expression. "
        "Use ONLY listed column names in expressions. Do not invent columns or "
        "COUNT(DISTINCT table_name) when table_name is not a column. "
        "Skip placeholder columns like column_2.\n\n"
        + "\n".join(schema_lines)
    )
    try:
        return _parse_llm_suggestions(client.complete(prompt))
    except Exception:  # noqa: BLE001 — one failed batch must not lose the others
        return None


def _parse_llm_suggestions(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        text = "\n".join(body).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _suggestion_is_grounded(
    *,
    expression: str,
    col_ref: Any,
    column_names: set[str],
    column_refs: set[str],
) -> bool:
    if col_ref and is_placeholder_column_name(str(col_ref).rsplit(".", 1)[-1]):
        return False
    if col_ref:
        token = str(col_ref).rsplit(".", 1)[-1]
        ref_l = str(col_ref).lower()
        if token.lower() in {c.lower() for c in column_names} or ref_l in {
            r.lower() for r in column_refs
        }:
            return True
    return expression_references_schema_column(
        expression, column_names=column_names, column_refs=column_refs
    )


def _apply_llm_suggestions(
    db: Session,
    *,
    tenant_id: UUID,
    connection_id: UUID,
    suggestions: dict[str, Any],
    column_names: set[str] | None = None,
    column_refs: set[str] | None = None,
) -> dict[str, int]:
    metric_names, dimension_names, entity_names = _existing_names(
        db, tenant_id, connection_id
    )
    counts = {"metrics": 0, "dimensions": 0, "entities": 0, "relationships": 0}
    names = column_names or set()
    refs = column_refs or set()

    for item in suggestions.get("metrics") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        expression = str(item.get("expression") or "").strip()
        col_ref = item.get("source_column_ref")
        if col_ref and _find_metric_by_ref(db, tenant_id, connection_id, str(col_ref)):
            continue
        if not name or not expression:
            continue
        if _find_metric_by_expression(db, tenant_id, connection_id, expression):
            continue
        if names and not _suggestion_is_grounded(
            expression=expression,
            col_ref=col_ref,
            column_names=names,
            column_refs=refs,
        ):
            continue
        p_schema, p_table, p_col = _physical_parts(str(col_ref or expression))
        unique = _unique_name(name, metric_names)
        metric = Metric(
            tenant_id=tenant_id,
            connection_id=connection_id,
            name=unique,
            expression=expression,
            label=item.get("label") or name,
            synonyms_json=encode_synonyms(item.get("label"), name, name=unique),
            status="suggested",
            source_column_ref=col_ref,
            physical_schema=p_schema,
            physical_table=p_table,
            physical_column=p_col,
        )
        if _flush_new(db, metric):
            counts["metrics"] += 1

    for item in suggestions.get("dimensions") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        expression = str(item.get("expression") or "").strip()
        col_ref = item.get("source_column_ref")
        if col_ref and _find_dimension_by_ref(db, tenant_id, connection_id, str(col_ref)):
            continue
        if not name or not expression:
            continue
        if _find_dimension_by_expression(db, tenant_id, connection_id, expression):
            continue
        if names and not _suggestion_is_grounded(
            expression=expression,
            col_ref=col_ref,
            column_names=names,
            column_refs=refs,
        ):
            continue
        p_schema, p_table, p_col = _physical_parts(str(col_ref or expression))
        unique = _unique_name(name, dimension_names)
        dimension = Dimension(
            tenant_id=tenant_id,
            connection_id=connection_id,
            name=unique,
            expression=expression,
            label=item.get("label") or name,
            synonyms_json=encode_synonyms(item.get("label"), name, name=unique),
            status="suggested",
            source_column_ref=col_ref,
            physical_schema=p_schema,
            physical_table=p_table,
            physical_column=p_col,
        )
        if _flush_new(db, dimension):
            counts["dimensions"] += 1

    return counts
