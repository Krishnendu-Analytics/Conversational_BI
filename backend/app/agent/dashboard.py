"""Build and edit multi-widget dashboards from discovered schema."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from app.agent.dashboard_filters import apply_dashboard_filters
from app.agent.measure_kind import is_code_like_name
from app.agent.file_dashboard_sql import (
    adapt_file_dashboard_spec,
    is_calendar_date_column,
    is_file_sql_engine,
    looks_like_duration_name,
    rewrite_sql_for_file_engine,
    sql_ident,
    sql_table_ref,
)
from app.agent.analytical_plan import (
    compile_analytical_plan,
    format_profile_for_prompt,
    heuristic_analytical_plan,
    parse_llm_analytical_plan,
    prune_invalid_widgets,
)
from app.agent.analysis_quality import evaluate_executed_widgets
from app.agent.dataset_profiler import (
    DatasetProfile,
    pick_join_dimension_tables,
    profile_dataset,
)
from app.schema_profiling import (
    build_table_stats_sql,
    merge_stats_into_table,
    parse_table_stats_row,
    qualified_table_name,
)
from app.agent.prompts import get_prompt
from app.tools.customer_db import run_readonly_query
from app.tools.sql_safety import SQLSafetyPipeline
from app.query_execution import QueryCategory, QueryExecutionError, user_facing_execution_error

# Framework / migration tables — not tenant business data.
_SYSTEM_TABLE_PREFIXES = ("auth_", "django_", "alembic_", "sqlite_")
# Columns that identify a dimension's entity (name / code / number).
_ENTITY_ID_TOKENS = {"name", "code", "number", "no", "id", "key"}
_BACKUP_TABLE_MARKERS = ("_bkp", "_backup", "_old")

_NUMERIC_HINTS = ("int", "numeric", "decimal", "float", "double", "real", "money", "number")
_TEXT_HINTS = (
    "name", "type", "status", "category", "region", "city", "segment", "channel",
    "agent", "user", "campaign", "client", "site", "portfolio", "telecaller",
)
_MEASURE_NAME_HINTS = (
    "amount", "total", "sum", "qty", "quantity", "count", "value",
    "revenue", "sales", "balance", "outstanding", "volume", "cost",
)

logger = logging.getLogger(__name__)

_STOP_WORDS = frozenset({
    "the", "and", "for", "show", "give", "create", "build", "make", "dashboard",
    "manager", "report", "full", "insight", "overview", "from", "with", "this",
    "that", "please", "about", "data", "database", "chart", "trend", "board",
})


def _qualified(table: dict[str, Any]) -> str | None:
    name = table.get("table_name")
    if not name:
        return None
    schema_name = table.get("schema_name") or ""
    return f"{schema_name}.{name}" if schema_name else str(name)


def _is_numeric(col: dict[str, Any]) -> bool:
    if col.get("is_pk"):
        return False
    dtype = str(col.get("data_type") or "").lower()
    if "bool" in dtype:
        return False
    return any(h in dtype for h in _NUMERIC_HINTS)


def _is_date(col: dict[str, Any]) -> bool:
    return is_calendar_date_column(col)


def _is_categorical(col: dict[str, Any]) -> bool:
    name = str(col.get("name") or "").lower()
    if col.get("is_pk") or name.endswith("_id") or name == "id":
        return False
    if looks_like_duration_name(name) or _is_date(col):
        return False
    dtype = str(col.get("data_type") or "").lower()
    if any(h in dtype for h in _NUMERIC_HINTS):
        return False
    if "bool" in dtype:
        return False
    return any(h in name for h in _TEXT_HINTS) or "char" in dtype or "text" in dtype


def _focus_tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in _STOP_WORDS
    }


def _table_name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[_\s]+", name.lower()) if len(t) > 2}


def _question_table_match_score(focus_text: str, table_name: str) -> int:
    q_tokens = _focus_tokens(focus_text)
    if not q_tokens:
        return 0
    t_tokens = _table_name_tokens(table_name)
    overlap = q_tokens & t_tokens
    if not overlap:
        return 0
    return len(overlap) * 60 + sum(len(t) for t in overlap)


def _table_schema_penalty(table: dict[str, Any]) -> int:
    bare = str(table.get("table_name") or "").lower()
    penalty = 0
    if any(marker in bare for marker in _BACKUP_TABLE_MARKERS):
        penalty += 400
    columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
    if not columns:
        return penalty + 300
    bool_count = sum(
        1 for c in columns if "bool" in str(c.get("data_type") or "").lower()
    )
    if len(columns) >= 8 and bool_count / len(columns) > 0.35:
        penalty += 600
    return penalty


def _measure_column_score(col: dict[str, Any]) -> int:
    name = str(col.get("name") or "").lower()
    if col.get("is_pk") or name.endswith("_id") or name == "id" or is_code_like_name(name):
        return -100
    if looks_like_duration_name(name):
        return -80
    if not _is_numeric(col):
        return -50
    score = 10
    for hint in _MEASURE_NAME_HINTS:
        if hint in name:
            score += 20
    return score


def _date_column_score(col: dict[str, Any]) -> int:
    if not _is_date(col):
        return -100
    name = str(col.get("name") or "").lower()
    score = 10
    if "date" in name:
        score += 20
    if name.endswith("_at") or name.startswith("date"):
        score += 8
    return score


def _categorical_column_score(col: dict[str, Any]) -> int:
    if not _is_categorical(col):
        return -100
    name = str(col.get("name") or "").lower()
    score = 10
    for hint in _TEXT_HINTS:
        if hint in name:
            score += 12
    if len(name) <= 3:
        score -= 10
    return score


def _rank_columns(columns: list[dict[str, Any]], scorer) -> list[dict[str, Any]]:
    ranked = sorted(
        [c for c in columns if isinstance(c, dict)],
        key=scorer,
        reverse=True,
    )
    return [c for c in ranked if scorer(c) > 0]


def _score_fact_table_candidates(
    schema: dict[str, Any], focus_text: str = ""
) -> list[tuple[int, dict[str, Any]]]:
    """Score every table by how "fact table"-like it looks (numeric/date-
    heavy, wide, matching the focus text) — shared by the single-table
    picker and the multi-table picker used for whole-database dashboards."""
    tables = [t for t in schema.get("tables") or [] if isinstance(t, dict)]
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        bare = str(table.get("table_name") or "").lower()
        if any(bare.startswith(p) for p in _SYSTEM_TABLE_PREFIXES):
            continue
        columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
        numeric = sum(1 for c in columns if _is_numeric(c))
        categorical = sum(1 for c in columns if _is_categorical(c))
        dates = sum(1 for c in columns if _is_date(c))
        score = (
            numeric * 4
            + categorical * 2
            + dates * 3
            + min(len(columns), 35)
            + _question_table_match_score(focus_text, bare)
            - _table_schema_penalty(table)
        )
        # A "fact_"/"f_" naming convention is a strong, explicit signal in
        # star-schema warehouses — nudge it ahead of generic scoring ties.
        if bare.startswith(("fact_", "f_")) or bare.endswith("_fact"):
            score += 25
        scored.append((score, table))
    return scored


def _pick_fact_table(schema: dict[str, Any], focus_text: str = "") -> dict[str, Any] | None:
    tables = [t for t in schema.get("tables") or [] if isinstance(t, dict)]
    if not tables:
        return None

    scored = _score_fact_table_candidates(schema, focus_text)
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    return tables[0]


def pick_fact_tables(
    schema: dict[str, Any], *, max_tables: int = 6, min_score: int = 10
) -> list[dict[str, Any]]:
    """The N most fact-table-like tables in the schema, for a whole-database
    dashboard that surveys every business domain rather than picking one.

    ``min_score`` filters out tables too thin to be a real fact table (e.g.
    lookup/reference tables with only a couple of columns) so the dashboard
    doesn't waste a section on them.
    """
    scored = _score_fact_table_candidates(schema)
    scored = [(s, t) for s, t in scored if s >= min_score]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:max_tables]]


def _find_table_by_name(schema: dict[str, Any], bare_name: str) -> dict[str, Any] | None:
    target = bare_name.lower()
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        if str(table.get("table_name") or "").lower() == target:
            return table
    return None


def _resolve_dashboard_table(
    dashboard: dict[str, Any],
    schema: dict[str, Any],
    focus_text: str = "",
) -> dict[str, Any] | None:
    source = str(dashboard.get("source_table") or "").lower()
    if source:
        found = _find_table_by_name(schema, source)
        if found is not None:
            return found
    return _pick_fact_table(schema, focus_text=focus_text)


def _date_bucket_expr(col: str, db_type: str) -> str:
    key = (db_type or "").lower()
    if is_file_sql_engine(key):
        return f"DATE_TRUNC('month', TRY_CAST({col} AS TIMESTAMP))"
    if key == "postgres":
        return f"DATE_TRUNC('month', {col})"
    if key == "mysql":
        return f"DATE_FORMAT({col}, '%Y-%m-01')"
    if key == "mssql":
        return f"DATEADD(MONTH, DATEDIFF(MONTH, 0, {col}), 0)"
    return col


def _validate_and_run(
    connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    sql: str,
    *,
    tenant_id: UUID,
    dashboard_id: str | None = None,
    use_cache: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    from app.config import get_settings
    from app.redis_client import query_result_cache

    ttl = get_settings().dashboard_result_cache_seconds if use_cache else 0
    candidates = [sql]
    if is_file_sql_engine(db_type):
        rewritten = rewrite_sql_for_file_engine(sql)
        if rewritten not in candidates:
            candidates.append(rewritten)
    last_exc: Exception | None = None
    for candidate in candidates:
        try:
            safe = SQLSafetyPipeline(schema=schema, db_type=db_type).validate(candidate)
            if ttl > 0:
                cached = query_result_cache.get(tenant_id, connection_id, safe)
                if cached is not None:
                    return cached, safe
            rows = run_readonly_query(
                connection_id,
                safe,
                tenant_id=tenant_id,
                category=QueryCategory.DASHBOARD,
                dashboard_id=dashboard_id,
            )
            # Always store fresh rows, so a Refresh also updates the cache.
            query_result_cache.set(
                tenant_id,
                connection_id,
                safe,
                rows,
                ttl_seconds=get_settings().dashboard_result_cache_seconds,
            )
            return rows, safe
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise QueryExecutionError("Dashboard widget query failed")


def _parse_connection_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _resolve_spec_connection(
    db: Any,
    *,
    spec: dict[str, Any],
    request_connection_id: str | None,
    tenant_id: UUID,
) -> str:
    """Unpersisted / in-memory spec — enforce request vs spec.connection_id match."""
    from sqlalchemy.exc import OperationalError

    from app.authorization import (
        DashboardConnectionMismatchError,
        resolve_dashboard_execution_connection,
    )

    spec_conn = spec.get("connection_id")
    spec_str = str(spec_conn).strip() if spec_conn else None
    request_str = str(request_connection_id).strip() if request_connection_id else None

    if spec_str:
        if request_str and request_str != spec_str:
            raise DashboardConnectionMismatchError()
        spec_uuid = _parse_connection_uuid(spec_str)
        if spec_uuid is not None:
            try:
                resolve_dashboard_execution_connection(
                    db,
                    dashboard={"connection_id": spec_str},
                    request_connection_id=spec_uuid,
                    tenant_id=tenant_id,
                )
            except (ValueError, OperationalError):
                pass
        return spec_str

    if not request_str:
        raise ValueError("connection_id is required for dashboard execution")
    request_uuid = _parse_connection_uuid(request_str)
    if request_uuid is not None:
        try:
            resolve_dashboard_execution_connection(
                db,
                dashboard={"connection_id": request_str},
                request_connection_id=request_uuid,
                tenant_id=tenant_id,
            )
        except (ValueError, OperationalError):
            pass
    return request_str


def _resolve_dashboard_sql_connection(
    spec: dict[str, Any],
    *,
    request_connection_id: str | None,
    tenant_id: UUID,
) -> str:
    """Derive authoritative connection from persisted dashboard; never trust caller alone."""
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from app.authorization import resolve_dashboard_execution_connection
    from app.database import SessionLocal
    from app.models import Dashboard

    dash_id = spec.get("id")
    request_uuid = _parse_connection_uuid(
        str(request_connection_id) if request_connection_id else None
    )

    db = SessionLocal()
    try:
        if dash_id:
            try:
                dashboard = db.scalar(
                    select(Dashboard).where(
                        Dashboard.id == UUID(str(dash_id)),
                        Dashboard.tenant_id == tenant_id,
                        Dashboard.deleted_at.is_(None),
                    )
                )
            except OperationalError:
                dashboard = None
            if dashboard is not None:
                authoritative = resolve_dashboard_execution_connection(
                    db,
                    dashboard=dashboard,
                    request_connection_id=request_uuid,
                    tenant_id=tenant_id,
                )
                return str(authoritative)
            return _resolve_spec_connection(
                db,
                spec=spec,
                request_connection_id=request_connection_id,
                tenant_id=tenant_id,
            )

        return _resolve_spec_connection(
            db,
            spec=spec,
            request_connection_id=request_connection_id,
            tenant_id=tenant_id,
        )
    finally:
        db.close()


def _dashboard_title(table: dict[str, Any], focus_text: str, question: str) -> str:
    bare = str(table.get("table_name") or "data")
    readable = bare.replace("_", " ").title()
    if _question_table_match_score(focus_text or question, bare) > 0:
        return f"{readable} Dashboard"
    if question.strip():
        return "Insight Dashboard"
    return f"{readable} Overview"


def _schema_subset(schema: dict[str, Any], focus_text: str, *, max_tables: int = 14) -> dict[str, Any]:
    """Return a smaller schema for LLM prompts (relevant tables first)."""
    tables = [t for t in schema.get("tables") or [] if isinstance(t, dict)]
    if len(tables) <= max_tables:
        return schema
    scored: list[tuple[int, dict[str, Any]]] = []
    for table in tables:
        bare = str(table.get("table_name") or "").lower()
        if any(bare.startswith(p) for p in _SYSTEM_TABLE_PREFIXES):
            continue
        columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
        score = (
            _question_table_match_score(focus_text, bare)
            + min(len(columns), 35)
            - _table_schema_penalty(table)
        )
        scored.append((score, table))
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [t for _, t in scored[:max_tables]]
    if not picked:
        picked = tables[:max_tables]
    return {**schema, "tables": picked}


def _format_schema_subset(schema: dict[str, Any], focus_text: str) -> str:
    """Compact schema text for dashboard planner prompts."""
    subset = _schema_subset(schema, focus_text)
    lines: list[str] = []
    for table in subset.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("table_name") or "?"
        schema_name = table.get("schema_name") or ""
        qualified = f"{schema_name}.{name}" if schema_name else str(name)
        cols = [
            f"{c.get('name')} {c.get('data_type')}"
            for c in (table.get("columns") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        if table.get("description"):
            lines.append(f"# {qualified}: {table['description']}")
        lines.append(f"{qualified}({', '.join(cols[:40])})")
    return "\n".join(lines) if lines else "No tables."


def _format_semantic_brief(semantic: dict[str, Any] | None) -> str:
    if not semantic:
        return "None configured."
    lines: list[str] = []
    for label, key in (("Metrics", "metrics"), ("Dimensions", "dimensions")):
        items = semantic.get(key) or []
        if not isinstance(items, list) or not items:
            continue
        lines.append(f"{label}:")
        for item in items[:12]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            expr = item.get("expression")
            if name and expr:
                lines.append(f"  - {name}: {expr}")
    return "\n".join(lines) if lines else "None configured."


def _parse_planner_json(raw: str) -> dict[str, Any] | None:
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
    if not isinstance(data, dict):
        return None
    widgets = data.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        return None
    return data


def _normalize_planner_spec(
    raw: dict[str, Any],
    *,
    schema: dict[str, Any],
    db_type: str,
) -> dict[str, Any] | None:
    """Validate planner widget SQL; drop invalid widgets."""
    pipeline = SQLSafetyPipeline(schema=schema, db_type=db_type)
    widgets: list[dict[str, Any]] = []
    for idx, item in enumerate(raw.get("widgets") or []):
        if not isinstance(item, dict):
            continue
        sql = str(item.get("sql") or "").strip()
        if not sql:
            continue
        try:
            safe = pipeline.validate(sql)
        except Exception:  # noqa: BLE001
            continue
        wtype = str(item.get("type") or "kpi").lower()
        chart = item.get("chart")
        if wtype == "chart" and chart not in {"bar", "line", "pie"}:
            chart = "bar"
        widgets.append(
            {
                "id": str(item.get("id") or f"widget-{idx}"),
                "type": "chart" if wtype == "chart" else "kpi",
                "title": str(item.get("title") or f"Widget {idx + 1}"),
                "sql": safe,
                "chart": chart if wtype == "chart" else None,
                "rows": [],
            }
        )
    if not widgets:
        return None
    return {
        "title": str(raw.get("title") or "Dashboard"),
        "source_table": raw.get("source_table"),
        "widgets": widgets,
        "build_mode": "semantic_llm" if raw else "heuristic",
    }


def _schema_for_dashboard(
    schema: dict[str, Any],
    focus_text: str = "",
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """Put the chosen fact table first, followed by any one-hop FK-connected
    dimension tables worth joining in, so the SQL compiler's FROM clause is
    correct and can build JOINs when an analysis spans both."""
    table = _pick_fact_table(schema, focus_text=focus_text)
    if table is None:
        return schema, None, []
    dim_tables = pick_join_dimension_tables(schema, table, focus_text)
    dim_ids = {id(t) for t in dim_tables}
    others = [
        t
        for t in (schema.get("tables") or [])
        if isinstance(t, dict) and t is not table and id(t) not in dim_ids
    ]
    return {**schema, "tables": [table, *dim_tables, *others]}, table, dim_tables


def _schema_has_column_stats(table: dict[str, Any] | None) -> bool:
    if not isinstance(table, dict):
        return False
    profiling = table.get("profiling") if isinstance(table.get("profiling"), dict) else {}
    stats = profiling.get("column_stats") if isinstance(profiling, dict) else {}
    if isinstance(stats, dict) and any(
        isinstance(v, dict) and v.get("distinct_count") is not None for v in stats.values()
    ):
        return True
    return any(
        isinstance(c, dict) and c.get("distinct_count") is not None
        for c in (table.get("columns") or [])
    )


def _enrich_schema_with_live_stats(
    schema: dict[str, Any],
    table: dict[str, Any],
    *,
    db_type: str,
    connection_id: str,
    tenant_id: UUID,
) -> dict[str, Any]:
    """Run one aggregation query so the planner sees real distinct/null/range stats."""
    if _schema_has_column_stats(table):
        return schema
    columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
    if not columns:
        return schema
    qualified = qualified_table_name(
        table.get("schema_name"),
        str(table.get("table_name") or ""),
        db_type,
    )
    try:
        sql, alias_map = build_table_stats_sql(qualified, columns, db_type)
        rows, _ = _validate_and_run(
            connection_id,
            schema,
            db_type,
            sql,
            tenant_id=tenant_id,
        )
        parsed = parse_table_stats_row(rows[0] if rows else None, alias_map)
        merge_stats_into_table(table, parsed)
    except Exception:  # noqa: BLE001
        logger.info("dashboard_live_stats_unavailable table=%s", table.get("table_name"))
    return schema


def _profile_for_dashboard(
    schema: dict[str, Any],
    *,
    db_type: str,
    focus_text: str,
    semantic: dict[str, Any] | None,
    connection_id: str | None = None,
    tenant_id: UUID | None = None,
) -> tuple[DatasetProfile, dict[str, Any]]:
    subset, table, dim_tables = _schema_for_dashboard(schema, focus_text)
    if connection_id and tenant_id is not None and table is not None:
        subset = _enrich_schema_with_live_stats(
            subset,
            table,
            db_type=db_type,
            connection_id=connection_id,
            tenant_id=tenant_id,
        )
    profile = profile_dataset(
        subset,
        db_type=db_type,
        focus_text=focus_text,
        primary_table=table,
        dimension_tables=dim_tables,
        semantic=semantic,
    )
    return profile, subset


def plan_dashboard_with_llm(
    *,
    schema: dict[str, Any],
    semantic: dict[str, Any] | None,
    question: str,
    focus_text: str,
    db_type: str,
    llm: Any,
    profile: DatasetProfile | None = None,
) -> dict[str, Any] | None:
    """LLM selects analyses from profiler candidates; SQL is compiled, not generated."""
    if profile is None:
        profile, schema = _profile_for_dashboard(
            schema,
            db_type=db_type,
            focus_text=focus_text,
            semantic=semantic,
        )
    catalog = format_profile_for_prompt(profile)
    prompt = get_prompt("dashboard_planner").render(
        db_type=db_type,
        focus_text=focus_text or "None.",
        question=question or "Create an insight dashboard",
        **catalog,
    )
    raw = llm.complete(prompt)
    plan = parse_llm_analytical_plan(raw, profile)
    if plan is None or not plan.analyses:
        return None
    spec = compile_analytical_plan(
        plan,
        profile=profile,
        schema=schema,
        db_type=db_type,
    )
    if not spec.get("widgets"):
        return None
    has_semantic = bool(
        (semantic or {}).get("metrics") or (semantic or {}).get("dimensions")
    )
    spec["build_mode"] = "semantic_llm" if has_semantic else "analytical_llm"
    return spec


def _pick_breakdown_column_profiled(
    connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    qualified: str,
    cat_cols: list[dict[str, Any]],
    *,
    tenant_id: UUID,
    max_probe: int = 4,
) -> dict[str, Any] | None:
    """Pick categorical column with useful DISTINCT count (2–500)."""
    if not cat_cols:
        return None
    best_col: dict[str, Any] | None = None
    best_distinct = 0
    for col in cat_cols[:max_probe]:
        name = str(col.get("name") or "")
        if not name:
            continue
        sql = (
            f"SELECT COUNT(DISTINCT {sql_ident(name, db_type)}) AS value "
            f"FROM {qualified} WHERE {sql_ident(name, db_type)} IS NOT NULL"
        )
        try:
            rows, _ = _validate_and_run(
                connection_id, schema, db_type, sql, tenant_id=tenant_id
            )
            val = rows[0].get("value") if rows else None
            distinct = int(val) if isinstance(val, (int, float)) else 0
        except Exception:  # noqa: BLE001
            continue
        if 2 <= distinct <= 500 and distinct > best_distinct:
            best_distinct = distinct
            best_col = col
    return best_col or cat_cols[0]


def _build_heuristic_dashboard_spec(
    schema: dict[str, Any],
    *,
    question: str = "",
    focus_text: str = "",
    db_type: str = "postgres",
    connection_id: str | None = None,
    tenant_id: UUID | None = None,
) -> dict[str, Any]:
    """Schema-driven widget plan with optional column profiling."""
    combined_focus = f"{focus_text} {question}".strip()
    table = _pick_fact_table(schema, focus_text=combined_focus)
    if table is None:
        return {"title": "Dashboard", "widgets": [], "source_table": None}

    raw_qualified = _qualified(table)
    if not raw_qualified:
        return {"title": "Dashboard", "widgets": [], "source_table": None}
    qualified = sql_table_ref(raw_qualified, db_type)

    bare = str(table.get("table_name") or "data")
    title = _dashboard_title(table, combined_focus, question)
    columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
    widgets: list[dict[str, Any]] = []

    widgets.append(
        {
            "id": "kpi-rows",
            "type": "kpi",
            "title": "Total Records",
            "sql": f"SELECT COUNT(*) AS value FROM {qualified}",
            "chart": None,
            "rows": [],
        }
    )

    numeric_cols = _rank_columns(columns, _measure_column_score)
    if numeric_cols:
        col = str(numeric_cols[0]["name"])
        col_sql = sql_ident(col, db_type)
        label = col.replace("_", " ").title()
        widgets.append(
            {
                "id": f"kpi-sum-{col}",
                "type": "kpi",
                "title": f"Total {label}",
                "sql": (
                    f"SELECT SUM({col_sql}) AS value FROM {qualified} "
                    f"WHERE {col_sql} IS NOT NULL"
                ),
                "chart": None,
                "rows": [],
            }
        )

    cat_cols = _rank_columns(columns, _categorical_column_score)
    breakdown_col: dict[str, Any] | None = None
    if cat_cols and connection_id and tenant_id is not None:
        breakdown_col = _pick_breakdown_column_profiled(
            connection_id,
            schema,
            db_type,
            qualified,
            cat_cols,
            tenant_id=tenant_id,
        )
    elif cat_cols:
        breakdown_col = cat_cols[0]
    if breakdown_col:
        col = str(breakdown_col["name"])
        col_sql = sql_ident(col, db_type)
        label = col.replace("_", " ").title()
        widgets.append(
            {
                "id": f"chart-{col}",
                "type": "chart",
                "title": f"By {label}",
                "sql": (
                    f"SELECT {col_sql} AS category, COUNT(*) AS value "
                    f"FROM {qualified} WHERE {col_sql} IS NOT NULL "
                    f"GROUP BY {col_sql} ORDER BY value DESC LIMIT 15"
                ),
                "chart": "bar",
                "rows": [],
            }
        )

    date_cols = _rank_columns(columns, _date_column_score)
    if date_cols:
        col = str(date_cols[0]["name"])
        col_sql = sql_ident(col, db_type)
        bucket = _date_bucket_expr(col_sql, db_type)
        measure = str(numeric_cols[0]["name"]) if numeric_cols else None
        if measure:
            measure_sql = sql_ident(measure, db_type)
            value_expr = f"SUM({measure_sql})"
            trend_title = f"{measure.replace('_', ' ').title()} Over Time"
        else:
            value_expr = "COUNT(*)"
            trend_title = "Trend Over Time"
        widgets.append(
            {
                "id": f"trend-{col}",
                "type": "chart",
                "title": trend_title,
                "sql": (
                    f"SELECT {bucket} AS period, {value_expr} AS value "
                    f"FROM {qualified} WHERE {col_sql} IS NOT NULL "
                    f"GROUP BY {bucket} ORDER BY period LIMIT 24"
                ),
                "chart": "line",
                "rows": [],
            }
        )

    if len(numeric_cols) > 1:
        col = str(numeric_cols[1]["name"])
        col_sql = sql_ident(col, db_type)
        widgets.append(
            {
                "id": f"avg-{col}",
                "type": "kpi",
                "title": f"Avg {col.replace('_', ' ').title()}",
                "sql": (
                    f"SELECT AVG({col_sql}) AS value FROM {qualified} "
                    f"WHERE {col_sql} IS NOT NULL"
                ),
                "chart": None,
                "rows": [],
            }
        )

    return {
        "title": title,
        "widgets": widgets,
        "source_table": bare,
        "build_mode": "heuristic",
    }


def build_dashboard_spec(
    schema: dict[str, Any],
    *,
    question: str = "",
    focus_text: str = "",
    db_type: str = "postgres",
    connection_id: str | None = None,
    semantic: dict[str, Any] | None = None,
    llm: Any | None = None,
    tenant_id: UUID | None = None,
    _retry: bool = True,
) -> dict[str, Any]:
    """Profile → analytical plan → compiled SQL widgets (heuristic if LLM fails).

    When even the no-LLM heuristic plan comes up empty, that's not just an
    unlucky LLM response — the heuristic reads the same dataset profile, so
    a double failure most likely means the profile itself was degraded for
    this one call (e.g. a transient hiccup profiling a remote DB). Retrying
    once with a freshly recomputed profile covers both a bad LLM response
    and a bad profile in one shot, rather than surfacing "no informative
    analyses" for something that would have worked on the very next try —
    confirmed by 11/11 successful rebuilds when manually retried in
    isolation after this same failure was observed live.
    """
    result = _build_dashboard_spec_once(
        schema,
        question=question,
        focus_text=focus_text,
        db_type=db_type,
        connection_id=connection_id,
        semantic=semantic,
        llm=llm,
        tenant_id=tenant_id,
    )
    if not result.get("widgets") and _retry:
        logger.warning(
            "build_dashboard_spec produced no widgets on first attempt "
            "(connection_id=%s, has_llm=%s) — retrying once with a fresh profile.",
            connection_id,
            llm is not None,
        )
        result = _build_dashboard_spec_once(
            schema,
            question=question,
            focus_text=focus_text,
            db_type=db_type,
            connection_id=connection_id,
            semantic=semantic,
            llm=llm,
            tenant_id=tenant_id,
        )
        if not result.get("widgets"):
            logger.warning(
                "build_dashboard_spec still produced no widgets after retry "
                "(connection_id=%s, has_llm=%s, fallback_reason=%s).",
                connection_id,
                llm is not None,
                result.get("fallback_reason"),
            )
    return result


def _build_dashboard_spec_once(
    schema: dict[str, Any],
    *,
    question: str = "",
    focus_text: str = "",
    db_type: str = "postgres",
    connection_id: str | None = None,
    semantic: dict[str, Any] | None = None,
    llm: Any | None = None,
    tenant_id: UUID | None = None,
) -> dict[str, Any]:
    combined_focus = f"{focus_text} {question}".strip()
    has_semantic = bool(
        isinstance(semantic, dict)
        and ((semantic.get("metrics") or semantic.get("dimensions")))
    )
    profile, subset = _profile_for_dashboard(
        schema,
        db_type=db_type,
        focus_text=combined_focus,
        semantic=semantic,
        connection_id=connection_id,
        tenant_id=tenant_id,
    )

    planned: dict[str, Any] | None = None
    llm_attempted = llm is not None
    if llm is not None:
        planned = plan_dashboard_with_llm(
            schema=subset,
            semantic=semantic,
            question=question,
            focus_text=combined_focus,
            db_type=db_type,
            llm=llm,
            profile=profile,
        )

    if not planned or not planned.get("widgets"):
        heuristic_plan = heuristic_analytical_plan(
            profile,
            question=question,
            focus_text=focus_text,
        )
        planned = compile_analytical_plan(
            heuristic_plan,
            profile=profile,
            schema=subset,
            db_type=db_type,
        )
        if llm_attempted:
            planned["build_mode"] = "analytical_heuristic_fallback"
            planned["planner_fallback"] = True
        elif has_semantic and llm is None:
            planned["build_mode"] = "heuristic_no_llm"
        else:
            planned["build_mode"] = planned.get("build_mode") or "analytical_heuristic"

    if planned.get("widgets"):
        return planned

    # Do not silently emit COUNT(*) / first-numeric dashboards when planning failed.
    return {
        "title": planned.get("title") or "Insight Dashboard",
        "widgets": [],
        "source_table": profile.source_table,
        "build_mode": "analytical_unavailable",
        "planner_fallback": True,
        "fallback_reason": (
            "No reliable analyses could be compiled from the dataset profile."
        ),
        "analysis_decisions": list(planned.get("analysis_decisions") or []),
        "analytical_plan": planned.get("analytical_plan"),
        "part_whole_pairs": list(planned.get("part_whole_pairs") or []),
    }


def _section_label(table: dict[str, Any]) -> str:
    """Human-readable domain name for a fact table's dashboard section
    (e.g. "fact_procurement" -> "Procurement")."""
    bare = re.sub(r"^(fact|f)_", "", str(table.get("table_name") or "").lower())
    return bare.replace("_", " ").title() or "Overview"


def table_domain_label(table: dict[str, Any]) -> str:
    """Public wrapper around ``_section_label`` for cross-module reuse
    (e.g. by ``app.agent.domain_catalog``) — same naming convention used
    for whole-database dashboard sections, kept in one place."""
    return _section_label(table)


def _build_dashboard_section(
    fact_table: dict[str, Any],
    schema: dict[str, Any],
    *,
    db_type: str,
    semantic: dict[str, Any] | None,
    connection_id: str | None,
    tenant_id: UUID | None,
    max_widgets: int = 3,
    avoid_dimensions: set[str] | None = None,
    label: str | None = None,
) -> list[dict[str, Any]]:
    """Build (uncompiled-SQL -> compiled) widgets for one fact table's
    domain, tagged with a "section" label — one slice of a whole-database
    dashboard rather than the single auto-picked table build_dashboard_spec
    produces.

    ``avoid_dimensions`` holds category columns earlier sections already
    broke down by: this section prefers a different one, so a database
    whose fact tables all join to the same product dimension doesn't render
    "... by Product Name" once per table."""
    dim_tables = pick_join_dimension_tables(schema, fact_table, "")
    dim_ids = {id(t) for t in dim_tables}
    others = [
        t
        for t in (schema.get("tables") or [])
        if isinstance(t, dict) and t is not fact_table and id(t) not in dim_ids
    ]
    subset = {**schema, "tables": [fact_table, *dim_tables, *others]}

    if connection_id and tenant_id is not None:
        subset = _enrich_schema_with_live_stats(
            subset,
            fact_table,
            db_type=db_type,
            connection_id=connection_id,
            tenant_id=tenant_id,
        )

    try:
        profile = profile_dataset(
            subset,
            db_type=db_type,
            focus_text="",
            semantic=semantic,
            primary_table=fact_table,
            dimension_tables=dim_tables,
        )
        if avoid_dimensions:
            def _reused(d: Any) -> bool:
                # "product_code" is the same entity as an already-used
                # "product_name" on the same dimension table.
                return (d.column or "").lower() in avoid_dimensions or (
                    f"{(d.table or '').lower()}#entity" in avoid_dimensions
                    and bool(set(re.split(r"[^a-z0-9]+", (d.column or "").lower())) & _ENTITY_ID_TOKENS)
                )

            fresh = [d for d in profile.dimensions if not _reused(d)]
            reused = [d for d in profile.dimensions if _reused(d)]
            if fresh:
                profile = profile.model_copy(update={"dimensions": [*fresh, *reused]})
        heuristic_plan = heuristic_analytical_plan(profile, question="", focus_text="")
        compiled = compile_analytical_plan(
            heuristic_plan,
            profile=profile,
            schema=subset,
            db_type=db_type,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "dashboard_section_build_failed table=%s", fact_table.get("table_name")
        )
        return []

    section = label or _section_label(fact_table)
    slug = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-") or "section"
    widgets = [dict(w) for w in (compiled.get("widgets") or []) if isinstance(w, dict)]
    for widget in widgets:
        widget["section"] = section
        # Different fact tables can compile to identically-named widget
        # ids (e.g. two tables both yielding "kpi-amount") — prefix by
        # section so ids stay unique across the combined dashboard.
        if widget.get("id"):
            widget["id"] = f"{slug}-{widget['id']}"
    return widgets[:max_widgets]


OVERVIEW_SECTION = "Overview"
_MAX_SEMANTIC_AREAS = 10


def _semantic_areas(
    schema: dict[str, Any], semantic: dict[str, Any] | None
) -> list[tuple[str, dict[str, Any], list[str]]]:
    """(area label, table, metric names) for every table the tenant's
    semantic model has metrics on — the business areas, named by the
    metrics' domain, most curated first. The model defines the areas, not
    table-name heuristics."""
    from collections import Counter

    from app.agent.dataset_profiler import _bare_table, _metric_belongs_to, _table_columns

    tables = {
        _bare_table(t.get("table_name")): t
        for t in (schema.get("tables") or [])
        if isinstance(t, dict) and t.get("table_name")
    }
    metrics = [
        m for m in ((semantic or {}).get("metrics") or [])
        if isinstance(m, dict) and (m.get("status") or "confirmed") in {"confirmed", "suggested"}
    ]
    by_table: dict[str, list[dict[str, Any]]] = {}
    for bare in tables:
        owned = [m for m in metrics if _metric_belongs_to(m, bare, _table_columns(tables[bare]))]
        if owned:
            by_table[bare] = owned
    ranked = sorted(
        by_table.items(),
        key=lambda item: (
            -sum(1 for m in item[1] if (m.get("status") or "confirmed") == "confirmed"),
            -len(item[1]),
        ),
    )
    areas: list[tuple[str, dict[str, Any], list[str]]] = []
    for bare, owned in ranked[:_MAX_SEMANTIC_AREAS]:
        domains = Counter(str(m.get("domain") or "").strip() for m in owned if m.get("domain"))
        domain = domains.most_common(1)[0][0] if domains else ""
        label = (domain[:1].upper() + domain[1:]) if domain else _section_label(tables[bare])
        areas.append((label, tables[bare], [str(m.get("name") or "") for m in owned if m.get("name")]))
    return areas


def _pick_headlines(areas: list[tuple[str, dict[str, Any], list[str]]], llm: Any) -> dict[str, str]:
    """{area: metric name}: the metric that best summarises each area, chosen
    by the LLM from the model's own metric names (one call for all areas).
    Empty without an LLM or on a bad reply — sections then lead with their
    first confirmed metric."""
    if llm is None or getattr(llm, "client", None) is None or not areas:
        return {}
    listing = "\n".join(
        f"Area: {label}\n" + "\n".join(f"  - {name}" for name in names[:40])
        for label, _, names in areas
    )
    try:
        from app.agent.document_dashboard import _parse_json

        data = _parse_json(llm.complete(get_prompt("dashboard_headlines").render(areas=listing))) or {}
    except Exception:  # noqa: BLE001 — headlines are a nicety, never a failure
        logger.warning("dashboard_headline_pick_failed", exc_info=True)
        return {}
    allowed = {label: {n.lower(): n for n in names} for label, _, names in areas}
    picked: dict[str, str] = {}
    for label, choice in data.items():
        name = allowed.get(str(label), {}).get(str(choice or "").strip().lower())
        if name:
            picked[str(label)] = name
    return picked


def _lead_with(semantic: dict[str, Any] | None, metric_name: str | None) -> dict[str, Any] | None:
    """The catalog with ``metric_name`` first, so a section is planned around
    it (headline KPI, breakdown, trend)."""
    if not semantic or not metric_name:
        return semantic
    metrics = list(semantic.get("metrics") or [])
    key = metric_name.lower()
    metrics.sort(key=lambda m: 0 if str((m or {}).get("name") or "").lower() == key else 1)
    return {**semantic, "metrics": metrics}


def build_full_database_dashboard_spec(
    schema: dict[str, Any],
    *,
    db_type: str = "postgres",
    connection_id: str | None = None,
    semantic: dict[str, Any] | None = None,
    tenant_id: UUID | None = None,
    max_fact_tables: int = 6,
    widgets_per_table: int = 6,
    llm: Any | None = None,
) -> dict[str, Any]:
    """Survey every business area of the database in one dashboard.

    Areas come from the tenant's semantic model (the tables its metrics
    measure, named by their domain), so a hospital gets Admissions and
    Billing and a bank gets Loans and Deposits — nothing industry-specific
    lives here. Fact-like tables the model does not cover yet get a
    structural section. Each area's headline KPI leads, in an Overview row.

    Deterministic/heuristic only (no LLM per area): one pass each keeps this
    fast for what the user asked as one request.
    """
    areas = _semantic_areas(schema, semantic)
    headlines = _pick_headlines(areas, llm)
    covered = {id(table) for _, table, _ in areas}
    structural = [
        (_section_label(t), t, [])
        for t in pick_fact_tables(schema, max_tables=max_fact_tables)
        if id(t) not in covered
    ]
    plan = [*areas, *structural][: max(max_fact_tables, len(areas))]
    if not plan:
        return {
            "title": "Full Database Insight Dashboard",
            "widgets": [],
            "build_mode": "full_database_unavailable",
            "planner_fallback": True,
            "fallback_reason": "No business areas or fact-like tables were found in this schema.",
        }

    overview: list[dict[str, Any]] = []
    widgets: list[dict[str, Any]] = []
    sections: list[str] = []
    # Sections are planned independently; a dimension shared by several
    # tables would otherwise yield the same widget in each. Drop repeats by
    # (type, title, expression) and prefer a fresh breakdown per section.
    seen_titles: set[tuple[str, str, str]] = set()
    used_dimensions: set[str] = set()
    for label, table, _names in plan:
        chosen = headlines.get(label)
        section_widgets = _build_dashboard_section(
            table,
            schema,
            db_type=db_type,
            semantic=_lead_with(semantic, chosen),
            connection_id=connection_id,
            tenant_id=tenant_id,
            max_widgets=widgets_per_table,
            avoid_dimensions=used_dimensions,
            label=label,
        )
        for widget in section_widgets:
            quality = widget.get("quality") if isinstance(widget.get("quality"), dict) else {}
            dim_col = str(quality.get("dimension_column") or "").lower()
            if dim_col:
                used_dimensions.add(dim_col)
                dim_table = str(quality.get("dimension_table") or "").lower()
                if dim_table and set(re.split(r"[^a-z0-9]+", dim_col)) & _ENTITY_ID_TOKENS:
                    used_dimensions.add(f"{dim_table}#entity")
        deduped: list[dict[str, Any]] = []
        for widget in section_widgets:
            expression = str(widget.get("metric_expression") or "").strip().lower()
            if expression and "." not in expression:
                expression = f"{str(table.get('table_name') or '').lower()}:{expression}"
            key = (
                str(widget.get("type") or ""),
                str(widget.get("title") or "").strip().lower(),
                expression,
            )
            if key[1] and key in seen_titles:
                continue
            seen_titles.add(key)
            deduped.append(widget)
        # The area's first KPI is its headline number: it leads the
        # dashboard in the Overview row instead of repeating in the section.
        headline = next(
            (w for w in deduped if w.get("type") == "kpi" and chosen and str(w.get("title") or "").lower() == chosen.lower()),
            next((w for w in deduped if w.get("type") == "kpi"), None),
        )
        if headline is not None:
            deduped.remove(headline)
            title = str(headline.get("title") or "")
            if label.lower() not in title.lower():
                title = f"{label}: {title}"  # several areas share the row
            overview.append({**headline, "title": title, "section": OVERVIEW_SECTION, "area": label})
        if deduped:
            widgets.extend(deduped)
            sections.append(label)

    if overview:
        widgets = [*overview, *widgets]
        sections.insert(0, OVERVIEW_SECTION)
    return {
        "title": "Full Database Insight Dashboard",
        "widgets": widgets,
        "sections": sections,
        "build_mode": "full_database_semantic" if areas else "full_database_heuristic",
    }


def _is_fact_like(table: dict[str, Any]) -> bool:
    """Fact tables by naming convention, or by carrying several foreign keys."""
    bare = str(table.get("table_name") or "").lower()
    if bare.startswith(("fact_", "f_", "fct_")) or bare.endswith("_fact"):
        return True
    fks = [fk for fk in table.get("foreign_keys") or [] if isinstance(fk, dict)]
    return len(fks) >= 2 and not bare.startswith(("dim_", "d_", "dimension_"))


def _merge_heuristic_widgets(
    planned: dict[str, Any],
    heuristic: dict[str, Any],
) -> dict[str, Any]:
    from app.agent.file_dashboard_sql import normalize_sql_key

    merged = dict(planned)
    widgets = [dict(w) for w in (planned.get("widgets") or []) if isinstance(w, dict)]
    seen = {normalize_sql_key(str(w.get("sql") or "")) for w in widgets}
    for extra in heuristic.get("widgets") or []:
        if not isinstance(extra, dict):
            continue
        if extra.get("error"):
            continue
        key = normalize_sql_key(str(extra.get("sql") or ""))
        if not key or key in seen:
            continue
        widgets.append(dict(extra))
        seen.add(key)
        if len([w for w in widgets if w.get("type") in {"kpi", "chart"}]) >= 5:
            break
    merged["widgets"] = widgets
    return merged


def _file_data_widget_count(dashboard: dict[str, Any]) -> int:
    return sum(
        1
        for widget in dashboard.get("widgets") or []
        if isinstance(widget, dict)
        and widget.get("type") in {"kpi", "chart", "pie", "table"}
        and not widget.get("error")
    )


def _prune_failed_file_widgets(dashboard: dict[str, Any]) -> dict[str, Any]:
    widgets = [w for w in dashboard.get("widgets") or [] if isinstance(w, dict)]
    data_ok = [
        w
        for w in widgets
        if w.get("type") in {"kpi", "chart"} and not w.get("error")
    ]
    if not data_ok:
        return dashboard
    kept: list[dict[str, Any]] = []
    for widget in widgets:
        if widget.get("type") in {"ai_insight", "text"}:
            kept.append(widget)
            continue
        if widget.get("error"):
            continue
        kept.append(widget)
    out = dict(dashboard)
    out["widgets"] = kept
    return out


def _widget_execution_error(exc: Exception) -> str:
    if isinstance(exc, QueryExecutionError):
        return exc.user_message
    return user_facing_execution_error(exc)


def _execute_dashboard_widget(
    widget: dict[str, Any],
    *,
    sql_connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    tenant_id: UUID,
    filters: list[dict[str, Any]],
    semantic: dict[str, Any] | None,
    source_table: str | None,
    dashboard_id: str | None,
    use_cache: bool = True,
) -> dict[str, Any]:
    out = dict(widget)
    sql = str(out.get("sql") or "")
    if not sql:
        return out
    try:
        filtered_sql = apply_dashboard_filters(
            sql,
            filters=filters,
            semantic=semantic,
            schema=schema,
            db_type=db_type,
            source_table=source_table,
        )
        rows, safe_sql = _validate_and_run(
            sql_connection_id,
            schema,
            db_type,
            filtered_sql,
            tenant_id=tenant_id,
            dashboard_id=dashboard_id,
            use_cache=use_cache,
        )
        out["sql"] = safe_sql
        out["rows"] = rows
        out.pop("data_warning", None)
        out.pop("stale", None)

        if out.get("type") == "kpi" and rows:
            first = rows[0]
            val = first.get("value")
            if val is not None:
                out["value"] = val
    except Exception as exc:  # noqa: BLE001
       
        out["error"] = _widget_execution_error(exc)
        out["rows"] = []
    return out


def _execute_dashboard_widgets_bounded(
    widget_specs: list[dict[str, Any]],
    *,
    sql_connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    tenant_id: UUID,
    filters: list[dict[str, Any]],
    semantic: dict[str, Any] | None,
    source_table: str | None,
    dashboard_id: str | None,
    max_widgets: int | None = None,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    from concurrent.futures import as_completed

    from app.config import get_settings
    from app.llm.usage import ContextThreadPoolExecutor

    settings = get_settings()
    limit = max_widgets if max_widgets is not None else settings.dashboard_max_widgets_per_refresh
    # The cap bounds how many queries one refresh runs. Widgets without SQL
    # (AI insight, text) cost nothing to keep and must never be cut off:
    # dropping a stored insight made a fresh one (new LLM call) every open.
    sql_widgets: list[tuple[int, dict[str, Any]]] = []
    passthrough: list[tuple[int, dict[str, Any]]] = []
    skipped = 0
    for idx, raw in enumerate(widget_specs):
        if not isinstance(raw, dict):
            continue
        widget = dict(raw)
        if not str(widget.get("sql") or ""):
            passthrough.append((idx, widget))
        elif len(sql_widgets) < limit:
            sql_widgets.append((idx, widget))
        else:
            # Beyond the cap: not re-queried this time, but still shown with
            # its last stored rows (flagged stale), never silently dropped.
            skipped += 1
            widget["stale"] = True
            passthrough.append((idx, widget))
    if skipped:
        logger.info(
            "dashboard_widget_cap applied total=%s cap=%s dashboard=%s",
            len(sql_widgets) + skipped,
            limit,
            (dashboard_id or "")[:8],
        )

    results: dict[int, dict[str, Any]] = {idx: w for idx, w in passthrough}
    max_workers = max(1, settings.dashboard_widget_max_concurrency)
    if len(sql_widgets) <= 1 or max_workers == 1:
        for idx, widget in sql_widgets:
            results[idx] = _execute_dashboard_widget(
                widget,
                sql_connection_id=sql_connection_id,
                schema=schema,
                db_type=db_type,
                tenant_id=tenant_id,
                filters=filters,
                semantic=semantic,
                source_table=source_table,
                dashboard_id=dashboard_id,
                use_cache=use_cache,
            )
    else:
        with ContextThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(
                    _execute_dashboard_widget,
                    widget,
                    sql_connection_id=sql_connection_id,
                    schema=schema,
                    db_type=db_type,
                    tenant_id=tenant_id,
                    filters=filters,
                    semantic=semantic,
                    source_table=source_table,
                    dashboard_id=dashboard_id,
                    use_cache=use_cache,
                ): idx
                for idx, widget in sql_widgets
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                results[idx] = future.result()

    return [results[i] for i in sorted(results)]


def execute_dashboard(
    spec: dict[str, Any],
    *,
    connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    tenant_id: UUID,
    semantic: dict[str, Any] | None = None,
    max_widgets: int | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Run each widget SQL and attach rows. With ``use_cache``, rows from
    the last few minutes are reused from Redis instead of re-querying."""
    sql_connection_id = _resolve_dashboard_sql_connection(
        spec,
        request_connection_id=connection_id,
        tenant_id=tenant_id,
    )
    filters = spec.get("filters") or []
    source_table = spec.get("source_table")
    dashboard_id = str(spec.get("id") or "") or None
    raw_widgets = [w for w in (spec.get("widgets") or []) if isinstance(w, dict)]
    widgets = _execute_dashboard_widgets_bounded(
        raw_widgets,
        sql_connection_id=sql_connection_id,
        schema=schema,
        db_type=db_type,
        tenant_id=tenant_id,
        filters=filters,
        semantic=semantic,
        source_table=source_table,
        dashboard_id=dashboard_id,
        max_widgets=max_widgets,
        use_cache=use_cache,
    )

    result = {
        "title": spec.get("title") or "Dashboard",
        "widgets": widgets,
        "source_table": source_table,
        "filters": filters,
        "build_mode": spec.get("build_mode"),
        "analysis_decisions": list(spec.get("analysis_decisions") or []),
        "analytical_plan": spec.get("analytical_plan"),
        "part_whole_pairs": list(spec.get("part_whole_pairs") or []),
        "dataset_row_count": spec.get("dataset_row_count"),
        "planner_fallback": spec.get("planner_fallback"),
        "fallback_reason": spec.get("fallback_reason"),
    }
    if spec.get("id"):
        result["id"] = spec["id"]
    if spec.get("layout"):
        result["layout"] = dict(spec["layout"])

    result = prune_invalid_widgets(result)
    result = evaluate_executed_widgets(result)

    from app.agent.dashboard_insight_service import (
        ensure_ai_insight_widget,
        refresh_ai_insight_widgets,
    )
    from app.llm.client import fast_llm, get_llm

    ensure_ai_insight_widget(result)
    refresh_ai_insight_widgets(result, semantic=semantic, llm=fast_llm(get_llm()))
    return result


class DashboardEditExecutionError(Exception):
    """Raised when a conversational edit cannot execute a modified widget."""


def execute_dashboard_edit(
    spec: dict[str, Any],
    *,
    connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    tenant_id: UUID,
    semantic: dict[str, Any] | None = None,
    modified_widget_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Execute dashboard widgets; strict failure for newly modified widgets."""
    sql_connection_id = _resolve_dashboard_sql_connection(
        spec,
        request_connection_id=connection_id,
        tenant_id=tenant_id,
    )
    modified = {str(wid) for wid in (modified_widget_ids or set())}
    filters = spec.get("filters") or []
    source_table = spec.get("source_table")
    dashboard_id = str(spec.get("id") or "") or None
    widgets: list[dict[str, Any]] = []

    for raw in spec.get("widgets") or []:
        if not isinstance(raw, dict):
            continue
        widget = dict(raw)
        wid = str(widget.get("id") or "")
        sql = str(widget.get("sql") or "")
        is_modified = wid in modified

        if not sql:
            if is_modified and widget.get("type") not in {"ai_insight", "text"}:
                raise DashboardEditExecutionError(
                    f"Widget '{widget.get('title')}' has no SQL after edit."
                )
            widgets.append(widget)
            continue

        try:
            filtered_sql = apply_dashboard_filters(
                sql,
                filters=filters,
                semantic=semantic,
                schema=schema,
                db_type=db_type,
                source_table=source_table,
            )
            rows, safe_sql = _validate_and_run(
                sql_connection_id,
                schema,
                db_type,
                filtered_sql,
                tenant_id=tenant_id,
                dashboard_id=dashboard_id,
            )
            widget["sql"] = safe_sql
            widget["rows"] = rows
            widget.pop("error", None)
            if widget.get("type") == "kpi" and rows:
                first = rows[0]
                val = first.get("value")
                if val is not None:
                    widget["value"] = val
        except Exception as exc:  # noqa: BLE001
            if is_modified:
                raise DashboardEditExecutionError(
                    f"Widget '{widget.get('title')}' failed: {_widget_execution_error(exc)}"
                ) from exc
            widget["error"] = _widget_execution_error(exc)
            widget["rows"] = []
        widgets.append(widget)

    result = {
        "title": spec.get("title") or "Dashboard",
        "name": spec.get("name") or spec.get("title") or "Dashboard",
        "widgets": widgets,
        "source_table": source_table,
        "filters": filters,
        "layout": dict(spec.get("layout") or {}),
        "build_mode": spec.get("build_mode"),
        "analysis_decisions": list(spec.get("analysis_decisions") or []),
        "analytical_plan": spec.get("analytical_plan"),
    }
    if spec.get("id"):
        result["id"] = spec["id"]

    from app.agent.dashboard_insight_service import refresh_ai_insight_widgets
    from app.llm.client import fast_llm, get_llm

    refresh_ai_insight_widgets(result, semantic=semantic, llm=fast_llm(get_llm()))
    return result


def build_dashboard(
    schema: dict[str, Any],
    *,
    connection_id: str,
    question: str = "",
    focus_text: str = "",
    db_type: str = "postgres",
    semantic: dict[str, Any] | None = None,
    llm: Any | None = None,
    tenant_id: UUID,
) -> dict[str, Any]:
    spec = build_dashboard_spec(
        schema,
        question=question,
        focus_text=focus_text,
        db_type=db_type,
        connection_id=connection_id,
        semantic=semantic,
        llm=llm,
        tenant_id=tenant_id,
    )
    from app.agent.dashboard_insight_service import ensure_ai_insight_widget

    ensure_ai_insight_widget(spec)
    executed = execute_dashboard(
        spec,
        connection_id=connection_id,
        schema=schema,
        db_type=db_type,
        tenant_id=tenant_id,
        semantic=semantic,
    )
    if spec.get("build_mode"):
        executed["build_mode"] = spec["build_mode"]
    if spec.get("planner_fallback") is not None:
        executed["planner_fallback"] = spec.get("planner_fallback")
    if spec.get("fallback_reason"):
        executed["fallback_reason"] = spec.get("fallback_reason")
    data_count = _file_data_widget_count(executed)
    if data_count == 0:
        executed["fallback_reason"] = executed.get("fallback_reason") or (
            "No informative analyses remained after validation. "
            "The dataset does not provide a reliable analytical basis for a dashboard."
        )
        if spec.get("build_mode") == "analytical_unavailable" or not spec.get("widgets"):
            executed["build_mode"] = "analytical_unavailable"
    return executed


def build_full_database_dashboard(
    schema: dict[str, Any],
    *,
    connection_id: str,
    db_type: str = "postgres",
    semantic: dict[str, Any] | None = None,
    tenant_id: UUID,
    max_fact_tables: int = 6,
    widgets_per_table: int = 6,
    llm: Any | None = None,
) -> dict[str, Any]:
    """Whole-database counterpart to build_dashboard: one section per
    business area instead of a single auto-picked table."""
    spec = build_full_database_dashboard_spec(
        schema,
        db_type=db_type,
        connection_id=connection_id,
        semantic=semantic,
        tenant_id=tenant_id,
        max_fact_tables=max_fact_tables,
        widgets_per_table=widgets_per_table,
        llm=llm,
    )
    from app.agent.dashboard_insight_service import ensure_ai_insight_widget

    ensure_ai_insight_widget(spec)
    # The default per-refresh cap (24) is sized for a single-table
    # dashboard — a whole-database survey intentionally builds more, so
    # size the cap to what was actually planned instead of silently
    # dropping whichever sections were built last.
    widget_cap = len(spec.get("widgets") or []) or None
    executed = execute_dashboard(
        spec,
        connection_id=connection_id,
        schema=schema,
        db_type=db_type,
        tenant_id=tenant_id,
        semantic=semantic,
        max_widgets=widget_cap,
    )
    executed["build_mode"] = spec.get("build_mode")
    if spec.get("sections"):
        executed["sections"] = spec["sections"]
    if spec.get("planner_fallback") is not None:
        executed["planner_fallback"] = spec.get("planner_fallback")
    if spec.get("fallback_reason"):
        executed["fallback_reason"] = spec.get("fallback_reason")
    return executed


def _kpi_value(widget: dict[str, Any]) -> Any:
    if widget.get("value") is not None:
        return widget.get("value")
    rows = widget.get("rows") or []
    if rows and isinstance(rows[0], dict):
        return rows[0].get("value")
    return None


def _append_trend_widget(
    widgets: list[dict[str, Any]],
    *,
    table: dict[str, Any],
    db_type: str,
    numeric_cols: list[dict[str, Any]],
    existing_ids: set[str],
) -> str | None:
    raw_qualified = _qualified(table)
    if not raw_qualified:
        return None
    qualified = sql_table_ref(raw_qualified, db_type)
    columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
    date_cols = _rank_columns(columns, _date_column_score)
    for date_col in date_cols:
        col = str(date_col["name"])
        widget_id = f"trend-{col}"
        if widget_id in existing_ids:
            continue
        col_sql = sql_ident(col, db_type)
        bucket = _date_bucket_expr(col_sql, db_type)
        measure = str(numeric_cols[0]["name"]) if numeric_cols else None
        value_expr = f"SUM({sql_ident(measure, db_type)})" if measure else "COUNT(*)"
        title = (
            f"{measure.replace('_', ' ').title()} Over Time"
            if measure
            else "Trend Over Time"
        )
        widgets.append(
            {
                "id": widget_id,
                "type": "chart",
                "title": title,
                "sql": (
                    f"SELECT {bucket} AS period, {value_expr} AS value "
                    f"FROM {qualified} WHERE {col_sql} IS NOT NULL "
                    f"GROUP BY {bucket} ORDER BY period LIMIT 24"
                ),
                "chart": "line",
                "rows": [],
            }
        )
        return col
    return None


def edit_dashboard_spec(
    dashboard: dict[str, Any],
    instruction: str,
    schema: dict[str, Any],
    db_type: str,
    *,
    focus_text: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Apply chat edits to an existing dashboard spec. Returns (spec, notes)."""
    notes: list[str] = []
    spec = {
        "title": dashboard.get("title") or "Dashboard",
        "source_table": dashboard.get("source_table"),
        "widgets": [
            dict(w) for w in dashboard.get("widgets") or [] if isinstance(w, dict)
        ],
    }
    text = (instruction or "").lower()
    widgets = spec["widgets"]
    combined_focus = f"{focus_text} {instruction}".strip()

    if any(w in text for w in ("remove chart", "remove trend", "hide chart")):
        spec["widgets"] = [w for w in widgets if w.get("type") != "chart"]
        notes.append("Removed chart widgets from the dashboard.")
        return spec, notes

    if "remove kpi" in text or "remove total" in text:
        spec["widgets"] = [w for w in widgets if w.get("type") != "kpi"]
        notes.append("Removed KPI widgets from the dashboard.")
        return spec, notes

    if any(w in text for w in ("add trend", "add line", "show trend", "over time")):
        table = _resolve_dashboard_table(spec, schema, focus_text=combined_focus)
        if table:
            columns = [c for c in (table.get("columns") or []) if isinstance(c, dict)]
            numeric_cols = _rank_columns(columns, _measure_column_score)
            existing_ids = {
                str(w.get("id") or "")
                for w in widgets
                if str(w.get("id") or "").startswith("trend-")
            }
            added_col = _append_trend_widget(
                widgets,
                table=table,
                db_type=db_type,
                numeric_cols=numeric_cols,
                existing_ids=existing_ids,
            )
            if added_col:
                notes.append(f"Added trend chart using `{added_col}`.")
            elif existing_ids:
                notes.append(
                    "Trend chart(s) already cover the available date columns. "
                    "Try *add breakdown* or *rename dashboard to '…'*."
                )
        return spec, notes

    if any(w in text for w in ("add breakdown", "add bar", "by category", "by type")):
        table = _resolve_dashboard_table(spec, schema, focus_text=combined_focus)
        if table:
            qualified = _qualified(table)
            cat_cols = _rank_columns(
                [c for c in (table.get("columns") or []) if isinstance(c, dict)],
                _categorical_column_score,
            )
            for cat_col in cat_cols:
                col = str(cat_col["name"])
                if any(w.get("id") == f"chart-{col}" for w in widgets):
                    continue
                if qualified:
                    col_sql = sql_ident(col, db_type)
                    table_sql = sql_table_ref(qualified, db_type)
                    widgets.append(
                        {
                            "id": f"chart-{col}",
                            "type": "chart",
                            "title": f"By {col.replace('_', ' ').title()}",
                            "sql": (
                                f"SELECT {col_sql} AS category, COUNT(*) AS value "
                                f"FROM {table_sql} WHERE {col_sql} IS NOT NULL "
                                f"GROUP BY {col_sql} ORDER BY value DESC LIMIT 15"
                            ),
                            "chart": "bar",
                            "rows": [],
                        }
                    )
                    notes.append(f"Added breakdown chart by `{col}`.")
                    break
            else:
                notes.append("No additional categorical columns available for a breakdown.")
        return spec, notes

    match = re.search(r"rename(?:\s+dashboard)?\s+to\s+[\"']([^\"']+)[\"']", text)
    if match:
        spec["title"] = match.group(1).strip().title()
        notes.append(f"Renamed dashboard to **{spec['title']}**.")
        return spec, notes

    if not notes:
        notes.append("No dashboard changes were applied for that instruction.")
    return spec, notes


def edit_dashboard(
    dashboard: dict[str, Any],
    instruction: str,
    *,
    connection_id: str,
    schema: dict[str, Any],
    db_type: str,
    focus_text: str = "",
    tenant_id: UUID,
    semantic: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    updated, notes = edit_dashboard_spec(
        dashboard,
        instruction,
        schema,
        db_type,
        focus_text=focus_text,
    )
    executed = execute_dashboard(
        updated,
        connection_id=connection_id,
        schema=schema,
        db_type=db_type,
        tenant_id=tenant_id,
        semantic=semantic,
    )
    return executed, notes


def dashboard_to_rows(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten chart widgets into rows for legacy consumers."""
    for widget in dashboard.get("widgets") or []:
        if not isinstance(widget, dict):
            continue
        if widget.get("type") == "chart":
            rows = widget.get("rows") or []
            if rows:
                return list(rows)
    for widget in dashboard.get("widgets") or []:
        if isinstance(widget, dict) and widget.get("type") == "kpi":
            val = _kpi_value(widget)
            if val is not None:
                return [{"metric": widget.get("title"), "value": val}]
    return []


def primary_chart_suggestion(dashboard: dict[str, Any]) -> str | None:
    for widget in dashboard.get("widgets") or []:
        if isinstance(widget, dict) and widget.get("type") == "chart":
            chart = widget.get("chart")
            if chart in {"bar", "line"}:
                return chart
    return "bar" if dashboard.get("widgets") else None


def best_table_match_score(focus_text: str, schema: dict[str, Any]) -> int:
    """Highest question↔table token overlap score in the schema."""
    best = 0
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        bare = str(table.get("table_name") or "")
        best = max(best, _question_table_match_score(focus_text, bare))
    return best
