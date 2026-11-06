"""LangGraph workflow stages. Call llm + tools from here, not FastAPI."""

from __future__ import annotations

import logging
import operator
import re
from typing import Annotated, Any, Literal, NamedTuple
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.agent.events import (
    DashboardUpdatedEvent,
    ErrorEvent,
    InsightReadyEvent,
    IntentDetectedEvent,
    QueryExecutedEvent,
    SqlGeneratedEvent,
)
from app.agent.dataset_context import (
    format_dataset_clarification_answer,
    is_file_backed_db_type,
    prepare_file_dataset_context,
    resolve_dataset_clarification,
)
from app.agent.dashboard import (
    build_dashboard,
    build_full_database_dashboard,
    dashboard_to_rows,
    execute_dashboard,
    primary_chart_suggestion,
)
from app.agent.dashboard_edit_pipeline import run_dashboard_edit_pipeline
from app.agent.document_dashboard import (
    build_document_dashboard,
    load_document_text,
    wants_document_dashboard,
)
from app.agent.dashboard_operation_schema import PendingDashboardOp
from app.agent.dashboard_widget_catalog import build_widget_catalog, catalog_to_dicts
from app.agent.analysis_state import (
    AnalysisState,
    SourceScope,
    format_analysis_state_context,
)
from app.agent.follow_up import convo_dict_from_state, resolve_follow_up, resolve_query_clarification
from app.agent.query_plan import parse_query_plan
from app.agent.query_plan_validation import (
    format_clarification_answer,
    normalize_query_plan,
    validate_query_plan,
    verify_sql_reflects_plan,
)
from app.agent.chat_history import answer_history_question, load_history_facts
from app.agent.source_router import SourceDecision, decide_source, find_candidate_documents
from app.agent.supervisor import (
    MAX_SUPERVISOR_FILES,
    DatabaseInfo,
    FileInfo,
    SupervisorResult,
    run_supervisor,
)
from app.agent.sql_compiler import compile_query_plan
from app.agent.prompts import get_prompt
from app.authorization import (
    DashboardConnectionMismatchError,
    resolve_dashboard_execution_connection,
)
from app.database import SessionLocal
from app.config import get_settings
from app.llm.client import LLMClient, fast_llm, get_llm
from app.models import Conversation, User
from app.services import memory_service
from app.services.conversation_state_service import ConversationStateService
from app.services.semantic_rules import expand_question, format_rules_for_prompt
from app.services.dashboard_service import (
    create_dashboard_from_spec,
    get_dashboard,
    save_version_snapshot,
    update_dashboard_from_spec,
)
from app.redis_client import (
    ConversationMemory,
    RedisPublisher,
    SchemaCache,
    SemanticCache,
    progress_publisher,
)
from app.query_execution import (
    QueryCategory,
    is_non_retryable_execution_error,
    user_facing_execution_error,
)
from app.tools.customer_db import run_readonly_query
from app.tools.rag import (
    DocumentRetriever,
    RAGPipeline,
    RAGRetrieveRequest,
    RAGRetrieveResponse,
    merge_sql_and_rag_for_insight,
)
from app.tools.sql_safety import SQLSafetyPipeline

logger = logging.getLogger(__name__)

# Cap rows included in query_executed progress payloads.
_ROWS_PREVIEW_LIMIT = 5

_VALID_INTENTS = frozenset({
    "schema", "sql", "rag", "hybrid", "greeting", "dashboard", "edit_dashboard",
    "investigate", "history",
})

_OVERVIEW_PHRASES = (
    "dashboard",
    "overview",
    "insight",
    "insights",
    "summary",
    "kpi",
    "metrics",
    "report",
)

_NUMERIC_TYPE_HINTS = (
    "int",
    "numeric",
    "decimal",
    "float",
    "double",
    "real",
    "money",
    "number",
)

_MISSING_SCHEMA_MSG = (
    "No discovered schema is available for this connection yet. "
    "Open Connections, click Discover schema, then ask again."
)


class WorkflowState(TypedDict, total=False):
    """Data shared between workflow stages.

    Path merge (T-57):
    - ``sql``: fills ``sql`` + ``rows``; ``rag_*`` stay empty.
    - ``rag``: fills ``rag_query`` / ``rag_chunks`` / ``rag_context``.
    - ``hybrid``: runs SQL then RAG; insight uses both.
    - ``schema``: fills ``rows`` from SchemaCache; no SQL execution.
    - ``greeting``: neither SQL nor RAG.
    """

    tenant_id: str
    user_id: str
    connection_id: str
    conversation_id: str | None
    run_id: str
    question: str
    intent: str  # sql | rag | hybrid | schema | greeting | dashboard | edit_dashboard | investigate
    sql: str | None
    rows: list[dict[str, Any]]
    dashboard: dict[str, Any] | None
    rag_query: str | None
    rag_chunks: list[dict[str, Any]]
    rag_context: str | None
    rag_insufficient: bool | None
    chart_suggestion: str | None
    dashboard_edit_notes: list[str]
    analysis_state: dict[str, Any]
    query_plan: dict[str, Any] | None
    query_clarification: dict[str, Any] | None
    dataset_clarification: dict[str, Any] | None
    dataset_id: str | None
    dataset_display_name: str | None
    answer: str
    error: str | None
    steps: Annotated[list[str], operator.add]
    chat_grounding: dict[str, Any] | None
    used_fallback_sql_label: str | None
    investigation_evidence: list[dict[str, Any]] | None
    investigation_domains_checked: list[str] | None
    # Documents attached to *this* message (not earlier turns).
    attached_document_ids: list[str]
    # Documents attached earlier in the conversation (still searchable).
    conversation_document_ids: list[str]
    # Where the user pointed the chat (the @ picker): auto | documents | database.
    source_scope: str
    # Set by the supervisor: run this specialist, skip intent classification.
    forced_intent: str
    # Set by the supervisor for dashboards: whole_database | focused.
    dashboard_scope: str


def _publisher() -> RedisPublisher:
    return progress_publisher


def _base_event_kwargs(state: WorkflowState) -> dict[str, Any]:
    return {
        "run_id": state.get("run_id") or "",
        "tenant_id": state.get("tenant_id") or "",
        "user_id": state.get("user_id") or "",
        "conversation_id": state.get("conversation_id"),
        "connection_id": state.get("connection_id") or None,
    }


def _emit(state: WorkflowState, event: Any) -> None:
    """Best-effort progress publish; never fail the workflow."""
    try:
        _publisher().publish_event(event)
    except Exception:  # noqa: BLE001
        pass


def _parse_intent_label(raw: str) -> str | None:
    """Map LLM output to a single intent token (first line / first word)."""
    if not raw:
        return None
    first = raw.strip().splitlines()[0].strip().lower()
    # Strip common punctuation / markdown.
    first = first.strip("`*\"'.,:;")
    token = first.split()[0] if first else ""
    token = token.strip("`*\"'.,:;")
    if token in _VALID_INTENTS:
        return token
    return None


def _looks_like_overview_question(question_lc: str) -> bool:
    return any(phrase in question_lc for phrase in _OVERVIEW_PHRASES)


def _conversation_focus_text(state: WorkflowState) -> str:
    """Combine prior user turn + current question for table/topic matching."""
    analysis = _get_analysis_state(state)
    parts: list[str] = []
    if analysis.last_user_question:
        parts.append(analysis.last_user_question.strip())
    question = (state.get("question") or "").strip()
    if question:
        parts.append(question)
    return " ".join(parts)


def _conversation_memory() -> ConversationMemory:
    return ConversationMemory()


def _get_analysis_state(state: WorkflowState) -> AnalysisState:
    raw = state.get("analysis_state")
    if isinstance(raw, AnalysisState):
        return raw
    if isinstance(raw, dict) and raw:
        try:
            return AnalysisState.model_validate(raw)
        except Exception:  # noqa: BLE001
            pass
    return AnalysisState()


def _sync_analysis_state(state: WorkflowState, analysis: AnalysisState) -> None:
    state["analysis_state"] = analysis.model_dump(mode="json")


def _apply_file_dataset_to_analytical_context(
    state: WorkflowState,
    schema: dict[str, Any],
    semantic: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any] | None, str | None]:
    """Return (schema, semantic, question, clarification, error)."""
    if not is_file_backed_db_type(str(schema.get("db_type") or "")):
        return schema, semantic or {}, state.get("question") or "", None, None

    tenant_raw = state.get("tenant_id") or ""
    conn_raw = state.get("connection_id") or ""
    if not tenant_raw or not conn_raw:
        return schema, semantic or {}, state.get("question") or "", None, None

    db = SessionLocal()
    try:
        ctx = prepare_file_dataset_context(
            db,
            tenant_id=UUID(str(tenant_raw)),
            connection_id=UUID(str(conn_raw)),
            schema=schema,
            semantic=semantic,
            analysis=_get_analysis_state(state),
            question=state.get("question") or "",
        )
    finally:
        db.close()

    _sync_analysis_state(state, ctx.analysis)
    if ctx.dataset is not None:
        state["dataset_id"] = str(ctx.dataset.id)
        state["dataset_display_name"] = ctx.dataset.display_name
    if ctx.effective_question:
        state["question"] = ctx.effective_question

    return (
        ctx.schema,
        ctx.semantic,
        ctx.effective_question or (state.get("question") or ""),
        ctx.clarification,
        ctx.error,
    )


def _format_kpi_value(value: Any, title: str) -> str:
    """Readable KPI value for the dashboard summary: 930.03B, 4.96%."""
    from decimal import Decimal

    from app.agent.dashboard_insight_facts import _as_number, _format_number

    if value is None:
        return "—"
    number = _as_number(float(value) if isinstance(value, Decimal) else value)
    if number is None:
        return str(value)
    if "%" in title:
        return f"{number:,.2f}%"
    return _format_number(number)


def _format_analysis_state_context_from_state(state: WorkflowState) -> str:
    return format_analysis_state_context(_get_analysis_state(state))


def _load_conversation_from_state(state: WorkflowState) -> dict[str, Any]:
    return convo_dict_from_state(_get_analysis_state(state))


def _resolve_active_dashboard_id(
    state: WorkflowState,
    analysis: AnalysisState,
) -> UUID | None:
    """Resolve the single active dashboard for edit_dashboard (never create new)."""
    if analysis.dashboard.dashboard_id:
        return analysis.dashboard.dashboard_id

    convo = _load_conversation_from_state(state)
    raw_dash = convo.get("dashboard")
    if isinstance(raw_dash, dict) and raw_dash.get("id"):
        try:
            return UUID(str(raw_dash["id"]))
        except (ValueError, TypeError):
            pass

    conv_id = state.get("conversation_id")
    user_id = state.get("user_id")
    if not conv_id or not user_id:
        return None

    db = SessionLocal()
    try:
        user = db.get(User, UUID(str(user_id)))
        conv_row = db.get(Conversation, UUID(str(conv_id)))
        if (
            user is not None
            and conv_row is not None
            and conv_row.tenant_id == user.tenant_id
            and conv_row.user_id == user.id
            and conv_row.dashboard_id
        ):
            return conv_row.dashboard_id
    finally:
        db.close()
    return None


def _persist_analysis_state(
    *,
    conversation_id: str | None,
    question: str,
    final: dict[str, Any],
    tenant_id: str | None = None,
    user_id: str | None = None,
    connection_id: str | None = None,
) -> None:
    if not conversation_id or not tenant_id or not user_id:
        return
    try:
        db = SessionLocal()
        try:
            user = db.get(User, UUID(str(user_id)))
            if user is None:
                return
            svc = ConversationStateService()
            svc.save(
                db,
                conversation_id=UUID(str(conversation_id)),
                user=user,
                question=question,
                final=final,
                connection_id=UUID(str(connection_id)) if connection_id else None,
            )
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — best-effort persistence
        pass





_FOLLOW_UP_WORDS = 12


def _ready_documents(tenant_id: str, ids: list[str]) -> dict[str, str]:
    """{id: filename} for the given document ids this tenant still has, ready."""
    if not ids or not tenant_id:
        return {}
    from app.models import Document

    try:
        wanted = [UUID(i) for i in ids]
        db = SessionLocal()
        try:
            rows = db.query(Document.id, Document.filename).filter(
                Document.tenant_id == UUID(tenant_id),
                Document.id.in_(wanted),
                Document.status == "ready",
            )
            return {str(r.id): str(r.filename) for r in rows}
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — never block a chat turn on this lookup
        return {i: "" for i in ids}


def _tenant_ready_documents(tenant_id: str, limit: int) -> tuple[list[tuple[str, str, str]], int]:
    """([(id, filename, uploaded date)], total) of the tenant's ready files,
    newest first — so the supervisor knows every file exists, not only the
    ones whose passages happened to match the question."""
    if not tenant_id:
        return [], 0
    from sqlalchemy import func

    from app.models import Document

    try:
        db = SessionLocal()
        try:
            where = (Document.tenant_id == UUID(tenant_id), Document.status == "ready")
            total = int(db.query(func.count(Document.id)).filter(*where).scalar() or 0)
            rows = (
                db.query(Document.id, Document.filename, Document.created_at)
                .filter(*where)
                .order_by(Document.created_at.desc())
                .limit(limit)
            )
            return [
                (str(r.id), str(r.filename), r.created_at.date().isoformat() if r.created_at else "")
                for r in rows
            ], total
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — never block a chat turn on this lookup
        logger.warning("tenant_documents_lookup_failed", exc_info=True)
        return [], 0


def _retrieval_query(question: str, analysis: Any) -> str:
    """A short follow-up ("what about export customers?") is searched together
    with the previous question, so it still finds the passage it refers to."""
    previous = analysis.get("last_user_question") if isinstance(analysis, dict) else None
    if previous and len(question.split()) <= _FOLLOW_UP_WORDS:
        return f"{previous}\n{question}"
    return question


def _documents_relevant(state: WorkflowState) -> bool:
    """Files attached this turn always count; files from earlier turns only
    when the best retrieved passage clears the follow-up similarity bar."""
    if state.get("attached_document_ids"):
        return True
    chunks = state.get("rag_chunks") or []
    best = max((float(c.get("score") or 0.0) for c in chunks if isinstance(c, dict)), default=0.0)
    return best >= get_settings().rag_followup_min_similarity


def _narrate_attached_answer(
    *,
    question: str,
    rag_context: str,
    database_answer: str,
    llm: LLMClient,
    conversation_context: str = "",
) -> str | None:
    """Answer a question asked *with* attached documents: the documents lead,
    the database result is secondary. None when the LLM is unavailable (the
    caller keeps its own answer)."""
    prompt = get_prompt("attached_document_answer").render(
        question=question,
        rag_context=rag_context,
        database_answer=database_answer or "(nothing)",
        conversation_context=conversation_context or "(start of conversation)",
    )
    try:
        raw = llm.complete(prompt)
    except Exception:  # noqa: BLE001
        return None
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return None
    return text


def _narrate_rag_answer(
    *, question: str, rag_context: str, llm: LLMClient, conversation_context: str = ""
) -> str:
    """Grounded document answer; never invent sources."""
    prompt = get_prompt("rag_answer").render(
        question=question,
        rag_context=rag_context,
        conversation_context=conversation_context or "(start of conversation)",
    )
    try:
        raw = llm.complete(prompt)
    except Exception:  # noqa: BLE001
        raw = ""
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        # Deterministic fallback: return bounded retrieved context
        return (
            "Based on the retrieved documents:\n\n"
            f"{rag_context[:4000]}\n\n"
            "_Answer limited to retrieved document context._"
        )
    return text


def _clean_sql(raw: str) -> str | None:
    """Strip markdown fences / stub noise; return None if unusable."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.lower().startswith("[llm stub]"):
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence and optional closing fence.
        body = lines[1:]
        if body and body[-1].strip().startswith("```"):
            body = body[:-1]
        if body and body[0].strip().lower() in {"sql", "postgresql", "mysql", "tsql"}:
            body = body[1:]
        text = "\n".join(body).strip()
    # Drop a leading "SQL:" label if the model adds one.
    if text.lower().startswith("sql:"):
        text = text[4:].strip()
    return text or None


# Fix common LLM mistake: INTERVAL '3' months -> INTERVAL '3 months'
_INTERVAL_UNIT_OUTSIDE_QUOTES = re.compile(
    r"INTERVAL\s+'(\d+)'\s+([a-zA-Z]+)",
    re.IGNORECASE,
)


_DIALECT_RULES: dict[str, str] = {
    "postgres": """PostgreSQL rules:
- INTERVAL syntax: INTERVAL '3 months' (unit inside the quotes). Never write
  INTERVAL '3' months.
- Date filters: use date/timestamp columns from the schema (e.g. order_date).
- Relative periods (replace col with the actual date column):
  - last quarter: col >= DATE_TRUNC('quarter', CURRENT_DATE) - INTERVAL '3 months'
    AND col < DATE_TRUNC('quarter', CURRENT_DATE)
  - this quarter: col >= DATE_TRUNC('quarter', CURRENT_DATE)
    AND col < DATE_TRUNC('quarter', CURRENT_DATE) + INTERVAL '3 months'
  - last month: col >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'
    AND col < DATE_TRUNC('month', CURRENT_DATE)
  - last 7 days: col >= CURRENT_DATE - INTERVAL '7 days'
- Casts: col::type or CAST(col AS type)""",
    "mysql": """MySQL rules:
- Date filters: use date/datetime columns from the schema.
- Relative periods:
  - last quarter: col >= DATE_SUB(DATE(CONCAT(YEAR(CURDATE()), '-',
    LPAD(QUARTER(CURDATE())*3-2, 2, '0'), '-01')), INTERVAL 3 MONTH)
    AND col < DATE(CONCAT(YEAR(CURDATE()), '-',
    LPAD(QUARTER(CURDATE())*3-2, 2, '0'), '-01'))
- INTERVAL syntax: INTERVAL 3 MONTH (no quotes around the number).
- Casts: CAST(col AS type)""",
    "mssql": """SQL Server rules:
- Date filters: use date/datetime columns from the schema.
- Relative periods:
  - last quarter: col >= DATEADD(QUARTER, DATEDIFF(QUARTER, 0, GETDATE()) - 1, 0)
    AND col < DATEADD(QUARTER, DATEDIFF(QUARTER, 0, GETDATE()), 0)
- Casts: CAST(col AS type) or CONVERT(type, col)
- Do not use LIMIT; TOP is added by the safety pipeline when needed.""",
    "duckdb": """DuckDB rules (CSV/Excel file datasets):
- INTERVAL syntax: INTERVAL '3 months' (unit inside the quotes).
- Date buckets: DATE_TRUNC('month', TRY_CAST(date_col AS TIMESTAMP)).
- Never DATE_TRUNC, SUM, or AVG duration/text columns stored as HH:MM:SS
  strings. Use COUNT(*) or numeric columns instead.
- Quote reserved identifiers: "user", "order", "date".
- Casts: TRY_CAST(col AS type) or CAST(col AS type).""",
}


def _dialect_rules_for_prompt(db_type: str) -> str:
    from app.source_types import SourceType

    key = (db_type or "").strip().lower()
    if SourceType.is_file_backed(key) or key == "duckdb":
        return _DIALECT_RULES["duckdb"]
    return _DIALECT_RULES.get(
        key,
        "Use standard SQL compatible with the database type above.",
    )


def _postprocess_sql(sql: str, db_type: str) -> str:
    """Normalize known LLM SQL mistakes before validation/execution."""
    text = (sql or "").strip()
    if not text:
        return text
    if (db_type or "").lower() == "postgres":
        text = _INTERVAL_UNIT_OUTSIDE_QUOTES.sub(
            r"INTERVAL '\1 \2'",
            text,
        )
    return text


def _format_column(col: dict[str, Any], *, with_description: bool = False) -> str:
    name = str(col.get("name") or "?")
    data_type = str(col.get("data_type") or "unknown")
    flags: list[str] = []
    if col.get("is_pk"):
        flags.append("pk")
    if col.get("nullable") is False:
        flags.append("not null")
    suffix = f" ({', '.join(flags)})" if flags else ""
    description = str(col.get("description") or "").strip() if with_description else ""
    if description:
        if len(description) > _COLUMN_DESCRIPTION_PROMPT_CHARS:
            description = description[: _COLUMN_DESCRIPTION_PROMPT_CHARS - 1].rstrip() + "…"
        suffix = f'{suffix} "{description}"'
    return f"{name} {data_type}{suffix}"


_SCHEMA_PROMPT_TABLE_LIMIT = 40
# Data-dictionary column descriptions are shown only for the tables most
# relevant to the question (every table still gets its one-line summary),
# so a fully documented connection doesn't multiply the prompt size.
_SCHEMA_PROMPT_DESCRIBED_TABLES = 6
_COLUMN_DESCRIPTION_PROMPT_CHARS = 160


def _table_relevance_score(question_tokens: set[str], table: dict[str, Any]) -> int:
    """Token overlap between the question and this table's name/columns."""
    if not question_tokens:
        return 0
    score = 0
    name_tokens = set(re.findall(r"[a-z0-9]+", str(table.get("table_name") or "").lower()))
    score += len(question_tokens & name_tokens) * 3
    description_tokens = set(
        re.findall(r"[a-z0-9]+", str(table.get("description") or "").lower())
    )
    score += len(question_tokens & description_tokens) * 2
    for col in table.get("columns") or []:
        if not isinstance(col, dict):
            continue
        col_tokens = set(re.findall(r"[a-z0-9]+", str(col.get("name") or "").lower()))
        score += len(question_tokens & col_tokens)
    return score


def _question_tokens(question: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9]+", (question or "").lower()) if len(tok) > 2}


def _format_schema_for_prompt(
    schema: dict[str, Any],
    *,
    question: str = "",
    max_tables: int = _SCHEMA_PROMPT_TABLE_LIMIT,
) -> str:
    """Compact typed schema listing for LLM prompts.

    The listing keeps the connection's own table order and carries no
    per-question detail, so it is byte-identical from one question to the
    next and the provider's prompt cache bills it at the cached rate. What
    the question makes relevant (which tables, their column descriptions)
    goes in ``_schema_focus_for_prompt`` after the question instead.

    Only when a connection has more than ``max_tables`` tables does the
    question pick which ones are listed (by token overlap), so a large
    connection doesn't blow the context budget.
    """
    all_tables = [t for t in (schema.get("tables") or []) if isinstance(t, dict)]
    total = len(all_tables)
    truncated = total > max_tables

    if truncated:
        q_tokens = _question_tokens(question)
        ranked = sorted(
            range(total),
            key=lambda i: _table_relevance_score(q_tokens, all_tables[i]),
            reverse=True,
        )
        keep = set(ranked[:max_tables])
        all_tables = [t for i, t in enumerate(all_tables) if i in keep]

    lines: list[str] = []
    for table in all_tables:
        name = table.get("table_name") or "?"
        schema_name = table.get("schema_name") or ""
        qualified = f"{schema_name}.{name}" if schema_name else str(name)
        table_description = str(table.get("description") or "").strip()
        if table_description:
            lines.append(f"# {qualified}: {table_description}")
        parts: list[str] = []
        for col in table.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name"):
                continue
            parts.append(_format_column(col, with_description=False))
        fks = table.get("foreign_keys") or []
        fk_bits: list[str] = []
        if isinstance(fks, list):
            for fk in fks:
                if not isinstance(fk, dict):
                    continue
                ref_table = fk.get("referred_table") or "?"
                ref_col = fk.get("referred_column") or "?"
                ref_schema = fk.get("referred_schema") or schema_name
                ref_qualified = (
                    f"{ref_schema}.{ref_table}" if ref_schema else str(ref_table)
                )
                fk_bits.append(
                    f"FK {fk.get('column')}->{ref_qualified}.{ref_col}"
                )
        line = f"{qualified}({', '.join(parts)})"
        if fk_bits:
            line = f"{line}; {'; '.join(fk_bits)}"
        lines.append(line)

    body = "\n".join(lines) if lines else str(schema)
    if truncated:
        note = (
            f"[Showing the {max_tables} tables most relevant to the "
            f"question, out of {total} total tables in this connection. "
            "If the table you need isn't listed, ask about it by name.]"
        )
        return f"{note}\n{body}"
    return body


def _schema_focus_for_prompt(schema: dict[str, Any], question: str) -> str:
    """The per-question part of the schema: the tables the question's words
    point at (most relevant first) and their data-dictionary column notes.

    Kept out of the cached schema listing and placed after the question.
    Naming the relevant tables also stops a vague question ("give me
    insight of sales data") from latching onto whichever unrelated table
    happens to be listed first (dim_customer instead of fact_sales)."""
    q_tokens = _question_tokens(question)
    if not q_tokens:
        return ""
    tables = [t for t in (schema.get("tables") or []) if isinstance(t, dict)]
    scored = [(_table_relevance_score(q_tokens, t), i, t) for i, t in enumerate(tables)]
    top = [t for score, _, t in sorted(scored, key=lambda x: (-x[0], x[1])) if score > 0]
    top = top[:_SCHEMA_PROMPT_DESCRIBED_TABLES]
    if not top:
        return ""

    def qualified(table: dict[str, Any]) -> str:
        name = table.get("table_name") or "?"
        schema_name = table.get("schema_name") or ""
        return f"{schema_name}.{name}" if schema_name else str(name)

    lines = ["Tables most relevant to this question: " + ", ".join(qualified(t) for t in top)]
    notes: list[str] = []
    for table in top:
        for col in table.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name"):
                continue
            description = " ".join(str(col.get("description") or "").split())
            if not description:
                continue
            if len(description) > _COLUMN_DESCRIPTION_PROMPT_CHARS:
                description = description[: _COLUMN_DESCRIPTION_PROMPT_CHARS - 1].rstrip() + "…"
            notes.append(f'- {qualified(table)}.{col["name"]}: {description}')
    if notes:
        lines.append("Column notes for these tables:")
        lines.extend(notes)
    return "\n".join(lines)


def _join_prompt_blocks(*blocks: str) -> str:
    return "\n\n".join(b for b in blocks if b and b.strip() and b.strip() != "None.") or "None."


_METRIC_DESCRIPTION_PROMPT_CHARS = 140
_METRIC_SYNONYMS_PROMPT = 5


def _format_metric_line(item: dict[str, Any]) -> str:
    line = f"  - {item['name']}: {item['expression']}"
    description = " ".join(str(item.get("description") or "").split())
    if description:
        if len(description) > _METRIC_DESCRIPTION_PROMPT_CHARS:
            description = description[: _METRIC_DESCRIPTION_PROMPT_CHARS - 1].rstrip() + "…"
        line += f" — {description}"
    synonyms = [str(s) for s in (item.get("synonyms") or []) if s][:_METRIC_SYNONYMS_PROMPT]
    if synonyms:
        line += f" (also: {', '.join(synonyms)})"
    return line


def _format_semantic_for_prompt(semantic: dict[str, Any] | None) -> str:
    """Structured semantic catalog for LLM prompts."""
    if not semantic:
        return "None configured."
    lines: list[str] = []
    metrics = semantic.get("metrics") or []
    if isinstance(metrics, list) and metrics:
        lines.append(
            "Metrics (when the user asks for these terms, use the expression):"
        )
        for item in metrics:
            if isinstance(item, dict) and item.get("name") and item.get("expression"):
                lines.append(_format_metric_line(item))
    dimensions = semantic.get("dimensions") or []
    if isinstance(dimensions, list) and dimensions:
        lines.append("Dimensions:")
        for item in dimensions:
            if isinstance(item, dict) and item.get("name") and item.get("expression"):
                lines.append(f"  - {item['name']}: {item['expression']}")
    lines.extend(format_rules_for_prompt(semantic.get("rules")))
    return "\n".join(lines) if lines else "None configured."


def _ranking_question(question: str, semantic: dict[str, Any] | None) -> str:
    """The question plus concepts named by its vocabulary synonyms, used only
    to rank schema tables ("vendors" should surface dim_supplier)."""
    return expand_question(question, (semantic or {}).get("rules"))



def _confirmed_semantic_catalog(catalog: dict[str, Any] | None) -> dict[str, Any]:
    """Drop suggested objects so chat never compiles unconfirmed metrics."""
    if not isinstance(catalog, dict):
        return {"metrics": [], "dimensions": [], "relationships": []}

    def _keep(item: Any) -> bool:
        if not isinstance(item, dict) or not item.get("name"):
            return False
        status = item.get("status")
        return status in (None, "confirmed")

    out = dict(catalog)
    out["metrics"] = [m for m in (catalog.get("metrics") or []) if _keep(m)]
    out["dimensions"] = [d for d in (catalog.get("dimensions") or []) if _keep(d)]
    return out


def _resolve_semantic(
    tenant_id: str,
    connection_id: str,
    semantic_cache: SemanticCache,
    *,
    include_suggested: bool = False,
) -> dict[str, Any]:
    """Semantic catalog filtered to the requester's department grants."""
    if not tenant_id or not connection_id:
        return {"metrics": [], "dimensions": [], "relationships": []}
    from app.access import scope_agent_semantic

    return scope_agent_semantic(
        _resolve_full_semantic(
            tenant_id, connection_id, semantic_cache, include_suggested=include_suggested
        ),
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


def _resolve_full_semantic(
    tenant_id: str,
    connection_id: str,
    semantic_cache: SemanticCache,
    *,
    include_suggested: bool = False,
) -> dict[str, Any]:
    """Redis first; on miss hydrate connection-scoped catalog from platform DB.

    ``include_suggested`` adds metrics drafted by AI/discovery or imported
    but not yet reviewed: dashboards use them as candidates and chat shows
    them to the SQL writer as reference formulas (chat still compiles
    confirmed metrics only)."""
    if not tenant_id or not connection_id:
        return {"metrics": [], "dimensions": [], "relationships": []}
    cached = semantic_cache.get(tenant_id, connection_id)
    flag = cached.get("_include_suggested") if cached is not None else None
    # Catalogs written by load_connection_semantic_catalog carry the flag; one
    # without it predates this option and is used as is.
    if cached is not None and (flag is None or bool(flag) == include_suggested):
        return cached
    try:
        from app.database import SessionLocal
        from app.services.semantic_service import load_connection_semantic_catalog

        db = SessionLocal()
        try:
            catalog = load_connection_semantic_catalog(
                db,
                UUID(str(tenant_id)),
                UUID(str(connection_id)),
                semantic_cache=semantic_cache,
                include_suggested=include_suggested,
            )
        finally:
            db.close()
        return catalog
    except Exception:  # noqa: BLE001
        return {"metrics": [], "dimensions": [], "relationships": []}


def _repair_sql(
    *,
    llm: LLMClient,
    db_type: str,
    schema: dict[str, Any],
    semantic_context: dict[str, Any] | None,
    question: str,
    sql: str,
    error: str,
) -> str | None:
    """One-shot LLM repair after validation or database errors."""
    ranking_question = _ranking_question(question, semantic_context)
    prompt = get_prompt("sql_repair").render(
        db_type=str(db_type or ""),
        schema=_join_prompt_blocks(
            _format_schema_for_prompt(schema, question=ranking_question),
            _schema_focus_for_prompt(schema, ranking_question),
        ),
        semantic_context=_format_semantic_for_prompt(semantic_context),
        question=question or "",
        sql=sql or "",
        error=error or "",
        dialect_rules=_dialect_rules_for_prompt(db_type),
    )
    repaired = _clean_sql(llm.complete(prompt))
    if repaired:
        repaired = _postprocess_sql(repaired, db_type)
    return repaired


def _qualified_table_name(table: dict[str, Any]) -> str | None:
    name = table.get("table_name")
    if not name:
        return None
    schema_name = table.get("schema_name") or ""
    return f"{schema_name}.{name}" if schema_name else str(name)


def _is_numeric_column(col: dict[str, Any]) -> bool:
    if col.get("is_pk"):
        return False
    name = str(col.get("name") or "").lower()
    # A surrogate/foreign key (product_key, customer_id, ...) is an
    # identifier, not a measure, even when it isn't the table's own
    # primary key — summing one produces a meaningless number.
    if (
        name in {"id", "key", "pk"}
        or name.endswith(("_id", "_key", "_pk", "_uuid"))
    ):
        return False
    dtype = str(col.get("data_type") or "").lower()
    return any(hint in dtype for hint in _NUMERIC_TYPE_HINTS)


def _is_date_column(col: dict[str, Any]) -> bool:
    from app.agent.file_dashboard_sql import is_calendar_date_column

    return is_calendar_date_column(col)


def _schema_table_names(schema: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        qualified = _qualified_table_name(table)
        if qualified:
            names.append(qualified)
    return names


_MAX_REFERENCE_METRICS = 60


def _reference_metric_hints(semantic_context: dict[str, Any] | None) -> list[str]:
    """The tenant's drafted/imported KPI formulas that are not confirmed yet:
    shown to the SQL writer as reference (they are never compiled as is)."""
    lines: list[str] = []
    for item in ((semantic_context or {}).get("reference_metrics") or [])[:_MAX_REFERENCE_METRICS]:
        if isinstance(item, dict) and item.get("name") and item.get("expression"):
            area = f" [{item['domain']}]" if item.get("domain") else ""
            lines.append(f"  - {item['name']}{area}: {item['expression']}")
    return lines


def _question_hints_for_prompt(
    question: str,
    schema: dict[str, Any],
    semantic_context: dict[str, Any] | None = None,
) -> str:
    """Extra LLM guidance: the tenant's own KPI formulas (drafted, not yet
    confirmed), plus guardrails for overview/dashboard-style questions."""
    question_lc = (question or "").lower()
    kpi_lines = _reference_metric_hints(semantic_context)
    kpi_block = (
        "KPI definitions from this connection's semantic model, not yet reviewed "
        "(when the question asks for one, use its formula as the metric and keep a "
        "ratio a ratio of sums at any grouping):\n"
        + "\n".join(kpi_lines)
        if kpi_lines
        else ""
    )
    if not _looks_like_overview_question(question_lc):
        return kpi_block or "None."

    table_names = _schema_table_names(schema)
    lines = [
        "Overview/dashboard mode:",
        "- Use ONLY table and column names from the schema above.",
        "- Do NOT assume CRM, hospital, restaurant, IT, or retail column names.",
        "- Prefer COUNT(*) or SUM/AVG of numeric non-pk columns.",
        "- Use at most two tables; JOIN only on FK relationships from the schema.",
    ]
    if table_names:
        lines.append(f"- Available tables: {', '.join(table_names)}")
    if kpi_block:
        lines.append(kpi_block)
    return "\n".join(lines)


def _column_relevance_score(question_tokens: set[str], col: dict[str, Any]) -> int:
    """Token overlap between the question and this column's name."""
    if not question_tokens:
        return 0
    col_tokens = set(re.findall(r"[a-z0-9]+", str(col.get("name") or "").lower()))
    return len(question_tokens & col_tokens)


class FallbackChoice(NamedTuple):
    """A heuristic fallback query, plus a plain-English label for it.

    The label names what was actually picked (a table, or a table + field)
    in ordinary words — no SQL — so a non-technical user can tell at a
    glance whether the guess matches what they meant, instead of being
    pointed at a query they can't read.
    """

    sql: str
    label: str


def _humanize(raw: str) -> str:
    from app.services.semantic_identity import humanize_identifier

    return humanize_identifier(raw) or raw


def _build_fallback_choice(
    schema: dict[str, Any], question: str = ""
) -> FallbackChoice | None:
    """Deterministic schema-safe SQL when the LLM cannot produce valid SQL.

    Prefers the table/column the question's own keywords point to over the
    structurally "richest" table (most columns/FKs) — picking an unrelated
    table here is the same silent-wrong-answer failure mode as guessing an
    unrelated metric in the query-plan compiler (see the row-listing
    backstop in query_plan.py). The richness heuristic still decides ties
    and fully generic questions ("show me something") with no keywords to
    match on.
    """
    tables = [
        t for t in schema.get("tables") or [] if isinstance(t, dict)
    ]
    if not tables:
        return None

    question_lc = (question or "").lower()
    is_overview = _looks_like_overview_question(question_lc)

    if is_overview and len(tables) > 1:
        parts: list[str] = []
        table_labels: list[str] = []
        for table in tables[:5]:
            qualified = _qualified_table_name(table)
            if not qualified:
                continue
            bare = str(table.get("table_name") or "table").replace("'", "''")
            parts.append(
                f"SELECT '{bare}' AS table_name, COUNT(*) AS row_count "
                f"FROM {qualified}"
            )
            table_labels.append(_humanize(str(table.get("table_name") or "table")))
        if parts:
            label = "record counts across " + ", ".join(table_labels)
            return FallbackChoice(sql=" UNION ALL ".join(parts), label=label)

    question_tokens = {
        tok for tok in re.findall(r"[a-z0-9]+", question_lc) if len(tok) > 2
    }

    best_table: dict[str, Any] | None = None
    best_score = -1
    for table in tables:
        columns = [
            c for c in (table.get("columns") or []) if isinstance(c, dict)
        ]
        numeric_cols = [c for c in columns if _is_numeric_column(c)]
        # Keyword overlap dominates the ranking whenever the question has
        # real keywords; richness only breaks ties or covers questions with
        # no matchable keywords at all.
        score = _table_relevance_score(question_tokens, table) * 100
        score += len(numeric_cols) * 2 + len(columns)
        if table.get("foreign_keys"):
            score += 1
        if score > best_score:
            best_score = score
            best_table = table

    if best_table is None:
        return None

    qualified = _qualified_table_name(best_table)
    if not qualified:
        return None

    table_label = _humanize(str(best_table.get("table_name") or "table"))

    columns = [
        c for c in (best_table.get("columns") or []) if isinstance(c, dict)
    ]
    numeric_cols = [c for c in columns if _is_numeric_column(c)]
    if numeric_cols:
        # Stable sort: ties (including the common "no keyword overlap at
        # all" case, all scores 0) keep the original first-numeric-column
        # order, preserving prior behavior when there's nothing to match.
        numeric_cols = sorted(
            numeric_cols,
            key=lambda c: _column_relevance_score(question_tokens, c),
            reverse=True,
        )
        col_name = str(numeric_cols[0]["name"])
        sql = (
            f"SELECT SUM({col_name}) AS total_{col_name} "
            f"FROM {qualified}"
        )
        return FallbackChoice(
            sql=sql,
            label=f"{table_label}: Total {_humanize(col_name)}",
        )
    return FallbackChoice(
        sql=f"SELECT COUNT(*) AS row_count FROM {qualified}",
        label=f"{table_label} record count",
    )


def _fallback_sql(schema: dict[str, Any], question: str = "") -> str | None:
    """Backward-compatible SQL-only accessor; see ``_build_fallback_choice``."""
    choice = _build_fallback_choice(schema, question)
    return choice.sql if choice else None


def _validate_sql(
    sql: str,
    schema: dict[str, Any],
    db_type: str,
) -> str:
    """Run the full safety pipeline; raise ValueError when invalid."""
    return SQLSafetyPipeline(schema=schema, db_type=db_type).validate(sql)


_TRAILING_LIMIT_ONE_RE = re.compile(r"\bLIMIT\s+1\s*(;?\s*)$", re.IGNORECASE)
_TOP_ONE_RE = re.compile(r"^(\s*SELECT\s+)TOP\s*\(?\s*1\s*\)?\s", re.IGNORECASE)
_ASKED_FOR_ONE_RE = re.compile(r"\b(top|first|single|only)\s*(1|one)\b|\bjust one\b", re.IGNORECASE)
_TOP_ONE_WIDENED = 5


def _widen_top_one(sql: str, question: str) -> str:
    """"Which city has the most customers?" -> ... ORDER BY n DESC LIMIT 1
    hides ties: with three cities at 1 customer each the answer claimed
    "Boston has the most". Fetch a few rows instead so the narrator can see
    (and say) it is a tie; it still names the leader. Only for grouped,
    ordered queries, and never when the user asked for exactly one."""
    upper = sql.upper()
    if "GROUP BY" not in upper or "ORDER BY" not in upper or _ASKED_FOR_ONE_RE.search(question or ""):
        return sql
    widened = _TRAILING_LIMIT_ONE_RE.sub(lambda m: f"LIMIT {_TOP_ONE_WIDENED}{m.group(1)}", sql)
    if widened == sql:
        widened = _TOP_ONE_RE.sub(lambda m: f"{m.group(1)}TOP {_TOP_ONE_WIDENED} ", sql)
    return widened


def _ensure_valid_sql(
    sql: str | None,
    *,
    schema: dict[str, Any],
    db_type: str,
    semantic_context: dict[str, Any] | None,
    question: str,
    llm: LLMClient,
) -> tuple[str | None, str | None, str | None]:
    """Validate LLM SQL, repair once, then fall back to schema-safe SQL.

    The third return value is the plain-English fallback label (see
    ``FallbackChoice``) when the heuristic guess was used, else None —
    callers use it to caveat the answer, since a heuristic guess is lower
    confidence than SQL the LLM grounded directly in the question.
    """
    if not sql:
        return None, (
            "The language model did not return usable SQL. "
            "Check LLM_API_KEY / model settings and try again."
        ), None

    last_error = "Invalid SQL"
    candidates: list[str | None] = [sql]

    try:
        candidates[0] = _validate_sql(sql, schema, db_type)
    except ValueError as exc:
        last_error = str(exc)
        candidates[0] = None

    if candidates[0] is not None:
        return candidates[0], None, None

    repaired = _repair_sql(
        llm=llm,
        db_type=db_type,
        schema=schema,
        semantic_context=semantic_context,
        question=question,
        sql=sql,
        error=last_error,
    )
    if repaired:
        try:
            return _validate_sql(repaired, schema, db_type), None, None
        except ValueError as exc:
            last_error = str(exc)

    choice = _build_fallback_choice(schema, question)
    if choice:
        try:
            return _validate_sql(choice.sql, schema, db_type), None, choice.label
        except ValueError as exc:
            last_error = str(exc)

    return None, (
        "Could not generate SQL that matches the discovered schema. "
        f"Last error: {last_error}"
    ), None


def _recover_sql_after_execution_error(
    *,
    sql: str,
    schema: dict[str, Any],
    db_type: str,
    semantic_context: dict[str, Any] | None,
    question: str,
    llm: LLMClient,
    execution_error: str,
) -> tuple[str | None, str | None]:
    """Repair the SQL from the database's own error, then fall back.

    Repair comes first: the failed query answered the actual question and
    usually needs a one-token fix (a cast, a column type). Trying the
    heuristic fallback first returned it straight away — so any database
    error on a detailed, filtered question produced an unrelated total
    ("Total Gross Sales Value") instead of the answer.

    Second return value mirrors ``_ensure_valid_sql``: the plain-English
    fallback label when the heuristic guess is what actually ran, else None.
    """
    choice = _build_fallback_choice(schema, question)

    repaired = _repair_sql(
        llm=llm,
        db_type=db_type,
        schema=schema,
        semantic_context=semantic_context,
        question=question,
        sql=sql,
        error=execution_error,
    )
    if repaired:
        try:
            validated = _validate_sql(repaired, schema, db_type)
            if validated.strip().lower() != sql.strip().lower():
                return validated, None
        except ValueError:
            pass

    if choice:
        try:
            return _validate_sql(choice.sql, schema, db_type), choice.label
        except ValueError:
            pass
    return None, None


def _schema_to_rows(schema: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_name = str(table.get("table_name") or "")
        columns = table.get("columns") or []
        if not isinstance(columns, list):
            columns = []
        col_summary = ", ".join(
            _format_column(c) for c in columns if isinstance(c, dict)
        )
        fks = table.get("foreign_keys") or []
        fk_summary = "—"
        if isinstance(fks, list) and fks:
            parts = []
            for fk in fks:
                if not isinstance(fk, dict):
                    continue
                parts.append(
                    f"{fk.get('column')} → {fk.get('referred_table')}."
                    f"{fk.get('referred_column')}"
                )
            if parts:
                fk_summary = "; ".join(parts)
        row: dict[str, Any] = {"table": table_name}
        if table.get("description"):
            row["description"] = str(table["description"])
        row.update(
            {
                "column_count": len(columns),
                "columns": col_summary,
                "foreign_keys": fk_summary,
            }
        )
        rows.append(row)
    return rows


_SCHEMA_DETAIL_RE = re.compile(
    r"\b(schema|columns?|fields?|data ?types?|foreign keys?|primary keys?|ddl)\b"
)
_SCHEMA_COUNT_OR_LIST_RE = re.compile(
    r"\b(how many|number of)\b|\b(name|names|list)\b.*\b(table|tables|tabes)\b"
)
_SUGGEST_QUESTIONS_RE = re.compile(
    r"\bquestions?\b.*\b(ask|asking|can|could|should|try)\b"
    r"|\b(suggest|example|sample|some|give me|tell me)\b.*\bquestions?\b"
    r"|\bwhat (can|could|should) i ask\b"
)
_SCHEMA_OVERVIEW_RE = re.compile(
    r"\b(about|overview|insights?|summar\w*|contain\w*|covers?|explain|describe"
    r"|understand|purpose|what (kind|kinds|type|types|sort) of|what can i (do|get|learn))\b"
)


def _wants_question_suggestions(question: str) -> bool:
    return bool(_SUGGEST_QUESTIONS_RE.search((question or "").lower()))


def _wants_schema_overview(question: str) -> bool:
    """A business-level "what is this database about / what can I ask"
    question — answered with a narrative, not a column-by-column dump."""
    q = (question or "").lower()
    if _wants_question_suggestions(q):
        return True
    if _SCHEMA_DETAIL_RE.search(q) or _SCHEMA_COUNT_OR_LIST_RE.search(q):
        return False
    return bool(_SCHEMA_OVERVIEW_RE.search(q))


def _requested_question_count(question: str, default: int = 10) -> int:
    match = re.search(r"\b(\d{1,2})\b", question or "")
    if not match:
        return default
    return max(3, min(20, int(match.group(1))))


def _schema_tables_for_overview(schema: dict[str, Any], max_columns: int = 30) -> str:
    lines: list[str] = []
    for table in schema.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("table_name") or "(unnamed)"
        desc = table.get("description") or ""
        cols = [
            str(c.get("name"))
            for c in (table.get("columns") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        col_text = ", ".join(cols[:max_columns]) + (" …" if len(cols) > max_columns else "")
        lines.append(f"- {name}" + (f" — {desc}" if desc else "") + f"; columns: {col_text}")
    return "\n".join(lines)


def _schema_overview_answer(
    schema: dict[str, Any],
    question: str,
    llm: LLMClient,
) -> str | None:
    """LLM narrative of what the database covers (or suggested questions);
    None when the LLM is unavailable so the caller falls back."""
    tables = _schema_tables_for_overview(schema)
    if not tables:
        return None
    prompt = get_prompt("schema_overview").render(
        question=question,
        tables=tables,
        question_count=_requested_question_count(question),
    )
    try:
        raw = llm.complete(prompt)
    except Exception:  # noqa: BLE001
        return None
    text = (raw or "").strip()
    if not text or text.lower().startswith("[llm stub]"):
        return None
    return text


def _format_schema_answer(
    schema: dict[str, Any],
    question: str = "",
) -> str:
    db_type = schema.get("db_type") or "unknown"
    tables = [
        table
        for table in (schema.get("tables") or [])
        if isinstance(table, dict)
    ]

    if not tables:
        return (
            f"A schema is cached for this {db_type} connection, but it has no "
            "tables. Re-run Discover schema if that looks wrong."
        )

    question_lc = re.sub(r"\s+", " ", (question or "").strip().lower())

    table_count = len(tables)
    column_count = sum(
        len(table.get("columns") or [])
        for table in tables
        if isinstance(table.get("columns") or [], list)
    )

    asks_tables = bool(
        re.search(
            r"\bhow many\b.*\b(table|tables|tabes)\b"
            r"|\bnumber of\b.*\b(table|tables|tabes)\b",
            question_lc,
        )
    )

    asks_columns = bool(
        re.search(
            r"\bhow many\b.*\b(column|columns)\b"
            r"|\bnumber of\b.*\b(column|columns)\b",
            question_lc,
        )
    )

    asks_both = asks_tables and asks_columns

    if asks_both:
        return (
            f"There are {table_count} tables and {column_count} columns "
            "in total."
        )

    if asks_tables:
        return f"There are {table_count} tables in the database."

    if asks_columns:
        return (
            f"There are {column_count} columns across "
            f"{table_count} tables."
        )

    asks_table_names = bool(
        re.search(
            r"\b(name|names|list|show|display)\b.*"
            r"\b(table|tables|tabes)\b",
            question_lc,
        )
    )

    if asks_table_names or _wants_schema_overview(question):
        table_names = [
            f"{table.get('table_name') or '(unnamed)'}"
            + (f" — {table['description']}" if table.get("description") else "")
            for table in tables
        ]

        return (
            f"There are {table_count} tables:\n\n"
            + "\n".join(f"- {name}" for name in table_names)
        )

    # Default: return the full discovered schema.
    lines = [
        f"Here is the discovered schema for this connection ({db_type}):",
        "",
    ]

    for table in tables:
        table_name = table.get("table_name") or "(unnamed)"
        if table.get("description"):
            lines.append(f"{table_name} — {table['description']}")
        else:
            lines.append(str(table_name))

        columns = table.get("columns") or []
        if isinstance(columns, list):
            for col in columns:
                if isinstance(col, dict):
                    lines.append(f"  - {_format_column(col, with_description=True)}")

        fks = table.get("foreign_keys") or []
        if isinstance(fks, list):
            for fk in fks:
                if not isinstance(fk, dict):
                    continue

                lines.append(
                    f"  - FK: {fk.get('column')} → "
                    f"{fk.get('referred_table')}."
                    f"{fk.get('referred_column')}"
                )

        lines.append("")

    return "\n".join(lines).strip()


def _resolve_schema(
    tenant_id: str,
    connection_id: str,
    schema_cache: SchemaCache,
) -> dict[str, Any] | None:
    """Schema the requester may see: the full cached schema filtered to their
    department grants (app.access)."""
    from app.access import scope_agent_schema

    return scope_agent_schema(
        _resolve_full_schema(tenant_id, connection_id, schema_cache),
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


def _resolve_full_schema(
    tenant_id: str,
    connection_id: str,
    schema_cache: SchemaCache,
) -> dict[str, Any] | None:
    """Redis first; on miss hydrate from schema_tables and re-cache."""
    schema = schema_cache.get(tenant_id, connection_id)
    if schema is not None:
        return schema
    if not connection_id:
        return None
    try:
        from app.database import SessionLocal
        from app.tools.customer_db import get_stored_schema

        db = SessionLocal()
        try:
            stored = get_stored_schema(db, UUID(str(connection_id)))
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(stored, dict) or not stored.get("tables"):
        return None

    try:
        schema_cache.set(tenant_id, connection_id, stored)
    except Exception:  # noqa: BLE001
        pass
    return stored


def generate_schema_grounded_sql(
    *,
    llm: LLMClient,
    db_type: str,
    schema: dict[str, Any],
    semantic_context: dict[str, Any] | None,
    question: str,
    conversation_context: str = "",
) -> tuple[str | None, bool]:
    """Ask the LLM to write SQL directly against the schema — not through a
    structured query plan — the middle tier between the deterministic plan
    compiler and the last-resort schema-safe fallback (``_build_fallback_choice``).

    This is what lets a real DB connection answer questions even before any
    semantic metric has been confirmed for it. Public so app.agent.domain_tool
    can give each investigation domain call the same schema-grounded tier the
    single-shot chat path already has — without it, a domain call that can't
    resolve a structured plan collapses straight to a bare COUNT(*)/SUM(one
    column), which is a much thinner answer than this tier can produce.

    Second return value is True when the LLM explicitly signaled there's no
    matching data for this question (the NO_MATCHING_DATA sentinel) — callers
    should treat that as "this schema has nothing relevant", not as an
    ordinary generation failure to retry or fall back from.
    """
    ranking_question = _ranking_question(question, semantic_context)
    prompt = get_prompt("sql_generation").render(
        db_type=str(db_type),
        schema=_format_schema_for_prompt(schema, question=ranking_question),
        semantic_context=_format_semantic_for_prompt(semantic_context),
        conversation_context=conversation_context,
        question=question,
        question_hints=_join_prompt_blocks(
            _schema_focus_for_prompt(schema, ranking_question),
            _question_hints_for_prompt(question, schema, semantic_context),
        ),
        dialect_rules=_dialect_rules_for_prompt(str(db_type)),
    )

    raw_sql = llm.complete(prompt)
    normalized_raw = re.sub(r"[^A-Z_]", "", (raw_sql or "").upper())
    if normalized_raw == "NO_MATCHING_DATA":
        return None, True

    sql = _clean_sql(raw_sql)
    if sql:
        sql = _postprocess_sql(sql, str(db_type))
    return sql, False


# --- Public re-exports for other agent modules ---------------------------
# app.agent.domain_tool (the single-domain query tool used by the planned
# multi-domain investigator) reuses these SQL-generation/execution building
# blocks directly rather than duplicating them, so a fix here — e.g. to SQL
# safety validation or fallback heuristics — applies everywhere it's used.
# Plain aliases, not new functions: the underscore-prefixed names above stay
# owned by this DAG's nodes and their existing tests.
resolve_schema_for_connection = _resolve_schema
resolve_semantic_for_connection = _resolve_semantic
filter_confirmed_semantic_catalog = _confirmed_semantic_catalog
clean_generated_sql = _clean_sql
postprocess_generated_sql = _postprocess_sql
ensure_valid_sql = _ensure_valid_sql
recover_sql_after_execution_error = _recover_sql_after_execution_error


def _route_with_attachments(
    intent: str, attached: list[str] | None, earlier: list[str] | None = None
) -> str:
    """A message that attaches files is about those files: always search them.

    The classifier reads only the question, so "what happens to a buyer two
    months late?" with a credit policy attached went to SQL alone and never
    looked at the document. A data question becomes ``hybrid`` (database and
    documents); anything else short of a dashboard/schema/investigation
    request is answered from the documents."""
    if attached:
        if intent == "sql":
            return "hybrid"
        if intent in {"greeting", "rag"}:
            return "rag"
        return intent
    # Files from earlier turns: still searched for data questions (the answer
    # only uses them if they turn out relevant), but "thanks" stays a greeting.
    if earlier and intent == "sql":
        return "hybrid"
    return intent


class HistoryAnswerer:
    """Answer questions about the user's own chats from their saved messages."""

    def run(self, state: WorkflowState) -> dict[str, Any]:
        question = state.get("question") or ""
        tenant_id = state.get("tenant_id") or ""
        user_id = state.get("user_id") or ""
        answer = "I can only look at your chat history when you're signed in."
        if tenant_id and user_id:
            try:
                db = SessionLocal()
                try:
                    facts = load_history_facts(db, tenant_id=UUID(tenant_id), user_id=UUID(user_id))
                finally:
                    db.close()
                answer = answer_history_question(question, facts, fast_llm(get_llm()))
            except Exception:  # noqa: BLE001
                logger.warning("chat_history_answer_failed", exc_info=True)
                answer = "I couldn't read your chat history just now. Please try again."
        return {"answer": answer, "steps": ["answer_history"]}


_SPECIALIST_LABELS = {
    "sql": "Data agent: querying the database",
    "rag": "Document agent: reading your files",
    "history": "History agent: reading your past chats",
    "schema": "Schema agent: reading the database structure",
    "dashboard": "Dashboard agent: building a dashboard",
    "edit_dashboard": "Dashboard agent: updating the dashboard",
    "investigate": "Investigator: checking related areas",
}


def _use_supervisor(analysis_state: dict[str, Any]) -> bool:
    """Supervisor mode, except while a turn is waiting on the user's reply to
    a clarification (those replies only make sense to the pipeline)."""
    if get_settings().agent_mode != "supervisor":
        return False
    if analysis_state.get("pending_query_clarification") or analysis_state.get(
        "pending_dataset_clarification"
    ):
        return False
    dashboard = analysis_state.get("dashboard") or {}
    return not (isinstance(dashboard, dict) and dashboard.get("pending_operation"))


def _merge_answered_from(parts: list[dict[str, Any]]) -> dict[str, Any]:
    documents: list[str] = []
    connection_ids: list[str] = []
    for part in parts:
        documents.extend(d for d in part.get("documents") or [] if d not in documents)
        connection_ids.extend(c for c in part.get("connection_ids") or [] if c not in connection_ids)
    return {
        "database": any(p.get("database") for p in parts),
        "documents": documents,
        "history": any(p.get("history") for p in parts),
        "connection_ids": connection_ids,
    }


# Follow-up context that belongs to one database and must not leak into a
# query against another (the previous SQL, its plan, results, dataset).
_CONNECTION_BOUND_STATE = (
    "last_sql",
    "query_plan",
    "result_summary",
    "semantic_focus",
    "dataset_id",
    "dataset_display_name",
    "pending_query_clarification",
    "pending_dataset_clarification",
)


def _tenant_databases(tenant_id: str, selected_id: str) -> list[DatabaseInfo]:
    """Every database connection of the tenant with a discovered schema (the
    selected one always), with its table names, selected first."""
    from app.models import Connection

    if not tenant_id:
        return []
    db = SessionLocal()
    try:
        rows = db.query(Connection.id, Connection.name, Connection.db_type).filter(
            Connection.tenant_id == UUID(tenant_id)
        ).all()
    finally:
        db.close()
    cache = SchemaCache()
    out: list[DatabaseInfo] = []
    for conn_id, name, db_type in rows:
        cid = str(conn_id)
        schema = _resolve_schema(tenant_id, cid, cache)
        tables = _schema_table_names(schema) if schema else []
        if not tables and cid != selected_id:
            continue  # not discovered yet: nothing the data agent could query
        out.append(DatabaseInfo(id=cid, name=str(name or "database"), db_type=str(db_type or ""), tables=tables, selected=cid == selected_id))
    out.sort(key=lambda d: not d.selected)
    return out


class _AutoRoute(NamedTuple):
    decision: SourceDecision
    filenames: dict[str, str]
    in_chat: set[str]


def _route_auto_source(
    question: str,
    *,
    tenant_id: str,
    connection_id: str,
    chat_documents: dict[str, str],
    analysis_state: dict[str, Any],
) -> _AutoRoute | None:
    """Auto mode's pick between the database and the tenant's files; None
    keeps the old behaviour (database, plus this chat's files when relevant)."""
    if not get_settings().source_router_enabled or not tenant_id or not question.strip():
        return None
    try:
        db = SessionLocal()
        try:
            candidates = find_candidate_documents(
                db, tenant_id=tenant_id, question=question, chat_documents=chat_documents
            )
        finally:
            db.close()
        if not candidates:
            return None
        schema = _resolve_schema(tenant_id, connection_id, SchemaCache()) if connection_id else None
        try:
            context = format_analysis_state_context(AnalysisState.model_validate(analysis_state))
        except Exception:  # noqa: BLE001
            context = "None."
        decision = decide_source(
            question,
            candidates=candidates,
            table_names=_schema_table_names(schema) if schema else [],
            conversation_context=context,
            llm=fast_llm(get_llm()),
        )
    except Exception:  # noqa: BLE001 — routing is best effort
        logger.warning("auto_source_route_failed", exc_info=True)
        return None
    if decision is None:
        return None
    logger.info(
        "auto_source_route source=%s docs=%s reason=%s",
        decision.source,
        decision.document_ids,
        decision.reason,
    )
    return _AutoRoute(
        decision=decision,
        filenames={c.id: c.filename for c in candidates},
        in_chat={c.id for c in candidates if c.in_chat},
    )


def _apply_source_scope(intent: str, source: str) -> str:
    """The user pointed the chat at a source with @: answer from it only.

    ``documents``: everything except small talk and dashboards is answered
    from the files (a dashboard request is built from them, since the scoped
    files arrive as ``attached_document_ids``). ``database``: never search
    documents."""
    if source == "documents" and intent not in {"greeting", "dashboard", "edit_dashboard"}:
        return "rag"
    if source == "database" and intent in {"rag", "hybrid"}:
        return "sql"
    return intent


def _resolve_source_scope(
    requested: dict[str, Any] | None, analysis_state: dict[str, Any]
) -> tuple[SourceScope, bool]:
    """(scope, explicit): the scope sent with this message, else the one the
    conversation already has (sticky), else auto."""
    if requested is not None:
        try:
            return SourceScope.model_validate(requested), True
        except Exception:  # noqa: BLE001 — a bad scope falls back to auto
            return SourceScope(), True
    try:
        return SourceScope.model_validate(analysis_state.get("source_scope") or {}), False
    except Exception:  # noqa: BLE001
        return SourceScope(), False


def _answered_from(final: dict[str, Any]) -> dict[str, Any]:
    """Which sources this answer actually used, for the "Answered from" tag."""
    intent = final.get("intent")
    dashboard = final.get("dashboard") if isinstance(final.get("dashboard"), dict) else None
    document_dashboard = bool(dashboard and dashboard.get("build_mode") == "document")

    names: list[str] = []
    if document_dashboard:
        names = [
            str(d.get("filename") or "")
            for d in dashboard.get("source_documents") or []
            if isinstance(d, dict)
        ]
    else:
        chunks = [c for c in final.get("rag_chunks") or [] if isinstance(c, dict)]
        found = bool(chunks) and not final.get("rag_insufficient")
        if found and (intent == "rag" or (intent == "hybrid" and _documents_relevant(final))):
            names = [str((c.get("metadata") or {}).get("document_name") or "") for c in chunks]
    documents = list(dict.fromkeys(n for n in names if n))

    database = bool(final.get("sql")) or intent in {"schema", "investigate"} or (
        intent in {"dashboard", "edit_dashboard"} and dashboard is not None and not document_dashboard
    )
    return {
        "database": database,
        "documents": documents,
        "history": intent == "history",
        "connection_ids": [str(final["connection_id"])] if database and final.get("connection_id") else [],
    }


class IntentClassifier:
    """Classify question as SQL, RAG, hybrid, schema, or greeting."""

    def run(self, state: WorkflowState) -> dict[str, Any]:
        analysis = _get_analysis_state(state)
        question = (state.get("question") or "").strip()
        pending = analysis.pending_dataset_clarification
        if pending and resolve_dataset_clarification(question, analysis) is not None:
            original = str(pending.get("original_question") or "").strip()
            if original:
                question = original
                state["question"] = original

        forced = state.get("forced_intent")
        if forced:
            # The supervisor already chose this specialist; don't second-guess.
            update = {"intent": forced, "steps": ["classify_intent"]}
            _emit(
                {**state, **update},
                IntentDetectedEvent(
                    **_base_event_kwargs(state),
                    intent=forced,
                    llm_intent=forced,
                    question=question,
                    message=_SPECIALIST_LABELS.get(forced, "Working on it"),
                ),
            )
            return update

        llm = fast_llm(get_llm())
        prompt = get_prompt("intent_classifier").render(
            question=question,
            conversation_context=_format_analysis_state_context_from_state(state),
        )
        try:
            raw = llm.complete(prompt).strip().lower()
        except Exception:  # noqa: BLE001 — a failed call must not lose the turn
            raw = ""
        llm_intent = _parse_intent_label(raw)

        # The model decides by meaning, in any language. Only the state of the
        # conversation — never the wording — may override it: a reply to a
        # clarification continues that data question, a proposed dashboard
        # change waits for its confirmation, and there is nothing to edit
        # without a dashboard. (English keyword rules used to overrule the
        # model here; they misrouted anything phrased differently.)
        has_dashboard = bool(analysis.dashboard.dashboard_id)
        if analysis.pending_dataset_clarification:
            # Resume what the turn that asked "which dataset?" was doing.
            resumed = str(analysis.last_intent or "")
            intent = resumed if resumed in _VALID_INTENTS else (llm_intent or "sql")
        elif analysis.pending_query_clarification:
            intent = "sql"
        elif has_dashboard and analysis.dashboard.pending_operation:
            intent = "edit_dashboard"
        else:
            intent = llm_intent or "sql"
        if intent == "edit_dashboard" and not has_dashboard:
            intent = "dashboard"

        source = state.get("source_scope") or "auto"
        if intent != "history":
            if source == "auto":
                intent = _route_with_attachments(
                    intent, state.get("attached_document_ids"), state.get("conversation_document_ids")
                )
            else:
                intent = _apply_source_scope(intent, source)

        update = {"intent": intent, "steps": ["classify_intent"]}
        merged = {**state, **update}
        _emit(
            merged,
            IntentDetectedEvent(
                **_base_event_kwargs(merged),
                intent=intent,
                llm_intent=llm_intent,
                question=question,
                message=f"Intent classified as {intent}",
            ),
        )
        return update


class SchemaDescriber:
    """Answer schema questions from SchemaCache (no SQL execution)."""

    def __init__(self, schema_cache: SchemaCache | None = None) -> None:
        self.schema_cache = schema_cache or SchemaCache()

    def run(self, state: WorkflowState) -> dict[str, Any]:
        tenant_id = state.get("tenant_id") or ""
        connection_id = state.get("connection_id") or ""
        question = (state.get("question") or "").strip()

        if not connection_id:
            update: dict[str, Any] = {
                "rows": [],
                "answer": (
                    "No database connection was selected, so I cannot describe "
                    "a schema."
                ),
                "error": "Missing connection_id for schema description.",
                "steps": ["describe_schema"],
            }
            self._emit_loaded(state, update)
            return update

        schema = _resolve_schema(tenant_id, connection_id, self.schema_cache)

        if schema is None:
            update = {
                "rows": [],
                "answer": _MISSING_SCHEMA_MSG,
                "error": None,
                "steps": ["describe_schema"],
            }
            self._emit_loaded(state, update)
            return update

        answer: str | None = None
        rows = _schema_to_rows(schema)
        if _wants_schema_overview(question):
            # A business overview is the answer; a column grid under it is noise.
            rows = []
            answer = _schema_overview_answer(schema, question, get_llm())

        update = {
            "rows": rows,
            "answer": answer or _format_schema_answer(schema, question),
            "sql": None,
            "error": None,
            "steps": ["describe_schema"],
        }

        self._emit_loaded(state, update)
        return update

    def _emit_loaded(self, state: WorkflowState, update: dict[str, Any]) -> None:
        merged = {**state, **update}
        rows = list(update.get("rows") or [])

        _emit(
            merged,
            QueryExecutedEvent(
                **_base_event_kwargs(merged),
                sql=None,
                row_count=len(rows),
                rows=rows,
                message="Schema loaded",
            ),
        )


_MAX_AUDITED_EVIDENCE_FACTS = 3


def _summarize_investigation_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact, audit-safe summary of one investigation's per-domain
    evidence: enough to reconstruct *what was checked and found* without
    persisting full row data or unbounded SQL/fact text into AuditLog."""
    summary: list[dict[str, Any]] = []
    for raw in evidence:
        if not isinstance(raw, dict):
            continue
        facts = raw.get("facts") or {}
        highlights = (
            facts.get("ranked_highlights") if isinstance(facts, dict) else None
        ) or []
        summary.append(
            {
                "domain": raw.get("domain"),
                "connection_id": raw.get("connection_id"),
                "ok": raw.get("error") is None and raw.get("facts") is not None,
                "error": raw.get("error"),
                "fallback_label": raw.get("fallback_label"),
                "sql": (raw.get("sql") or "")[:500] or None,
                "highlights": list(highlights)[:_MAX_AUDITED_EVIDENCE_FACTS],
            }
        )
    return summary


def _record_investigation_audit(
    *,
    tenant_id: str,
    user_id: str | None,
    conversation_id: str | None,
    run_id: str | None,
    question: str,
    connection_ids: list[str],
    evidence: list[dict[str, Any]],
) -> None:
    """Best-effort append to the audit trail for a completed investigation —
    the only place in app.agent.* that writes to AuditLog today, closing the
    gap where a multi-domain investigation's evidence otherwise vanished
    once the response was sent (nothing under app.agent previously called
    log_audit at all)."""
    try:
        from app.audit import log_agent_audit

        db = SessionLocal()
        try:
            log_agent_audit(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                action="agent.investigation",
                resource_type="conversation",
                resource_id=conversation_id,
                metadata={
                    "run_id": run_id,
                    "question": question,
                    "connection_ids": connection_ids,
                    "domains_checked": sorted(
                        {
                            str(e.get("domain"))
                            for e in evidence
                            if isinstance(e, dict) and e.get("domain")
                        }
                    ),
                    "evidence": _summarize_investigation_evidence(evidence),
                },
            )
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


class Investigator:
    """Multi-domain root-cause investigation — thin adapter onto the
    cyclic investigation graph in app.agent.investigator, kept isolated
    from this DAG (see that module's docstring for the full design).

    The import is deferred to inside ``run`` rather than at module level:
    app.agent.investigator imports several public re-exports from this
    module, so a top-level import here would be circular.
    """

    def run(self, state: WorkflowState) -> dict[str, Any]:
        from app.agent.investigator import run_investigation
        from app.services.conversation_connections_service import (
            resolve_investigation_connection_ids,
        )

        question = (state.get("question") or "").strip()
        tenant_id = state.get("tenant_id") or ""
        connection_id = state.get("connection_id") or ""

        if not connection_id:
            return {
                "answer": (
                    "No database connection was selected, so I cannot "
                    "investigate this."
                ),
                "error": "Missing connection_id for investigation.",
                "steps": ["investigate"],
            }

        # Just the primary connection unless the conversation has extra
        # ones explicitly attached (Phase 4) — multi-connection is opt-in,
        # so this degrades to exactly the single-connection behavior above
        # whenever nothing extra is attached, or the lookup itself fails.
        conversation_id = state.get("conversation_id")
        connection_ids = [connection_id]
        if conversation_id and tenant_id:
            try:
                db = SessionLocal()
                try:
                    connection_ids = resolve_investigation_connection_ids(
                        db,
                        tenant_id=UUID(str(tenant_id)),
                        conversation_id=UUID(str(conversation_id)),
                        primary_connection_id=connection_id,
                    )
                finally:
                    db.close()
            except Exception:  # noqa: BLE001
                connection_ids = [connection_id]

        result = run_investigation(
            question,
            tenant_id=tenant_id,
            connection_ids=connection_ids,
            run_id=state.get("run_id"),
            conversation_id=conversation_id,
            user_id=state.get("user_id"),
        )

        answer = result.get("answer") or (
            "I could not complete an investigation for this question."
        )
        evidence = result.get("evidence") or []
        domains_checked = sorted(
            {
                str(e.get("domain"))
                for e in evidence
                if isinstance(e, dict) and e.get("domain")
            }
        )

        _record_investigation_audit(
            tenant_id=tenant_id,
            user_id=state.get("user_id"),
            conversation_id=conversation_id,
            run_id=result.get("run_id") or state.get("run_id"),
            question=question,
            connection_ids=connection_ids,
            evidence=evidence,
        )

        return {
            "answer": answer,
            "error": None,
            "steps": ["investigate"],
            "investigation_evidence": evidence,
            "investigation_domains_checked": domains_checked,
        }


class SQLGenerator:
    """Generate SQL using cached schema and semantic context."""

    def __init__(
        self,
        schema_cache: SchemaCache | None = None,
        semantic_cache: SemanticCache | None = None,
        llm: LLMClient | None = None,
    ) -> None:
        self.schema_cache = schema_cache or SchemaCache()
        self.semantic_cache = semantic_cache or SemanticCache()
        self.llm = llm or get_llm()

    def run(self, state: WorkflowState) -> dict[str, Any]:
        tenant_id = state.get("tenant_id") or ""
        connection_id = state.get("connection_id") or ""
        question = state.get("question") or ""

        schema = _resolve_schema(tenant_id, connection_id, self.schema_cache)

        if schema is None:
            update: dict[str, Any] = {
                "sql": None,
                "error": _MISSING_SCHEMA_MSG,
                "steps": ["generate_sql"],
            }
            merged = {**state, **update}

            _emit(
                merged,
                SqlGeneratedEvent(
                    **_base_event_kwargs(merged),
                    sql=None,
                    message="SQL generation skipped: schema not cached",
                ),
            )

            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    step="generate_sql",
                    error=update["error"],
                    message=update["error"],
                ),
            )
            return update

        semantic_context = _resolve_semantic(
            tenant_id,
            connection_id,
            self.semantic_cache,
            include_suggested=True,
        )
        reference_metrics = [
            m
            for m in (semantic_context.get("metrics") or [])
            if isinstance(m, dict) and m.get("status") == "suggested"
        ]
        semantic_context = _confirmed_semantic_catalog(semantic_context)

        schema, semantic_context, question, dataset_clarification, dataset_error = (
            _apply_file_dataset_to_analytical_context(
                state,
                schema,
                semantic_context,
            )
        )
        semantic_context = {**semantic_context, "reference_metrics": reference_metrics}

        if dataset_error:
            update = {
                "sql": None,
                "error": dataset_error,
                "answer": dataset_error,
                "steps": ["generate_sql"],
            }

            merged = {**state, **update}

            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    step="generate_sql",
                    error=dataset_error,
                    message=dataset_error,
                ),
            )

            return update

        if dataset_clarification is not None:
            answer = format_dataset_clarification_answer(
                dataset_clarification
            )

            update = {
                "sql": None,
                "dataset_clarification": dataset_clarification,
                "answer": answer,
                "error": None,
                "steps": ["generate_sql"],
            }

            merged = {**state, **update}

            _emit(
                merged,
                SqlGeneratedEvent(
                    **_base_event_kwargs(merged),
                    sql=None,
                    message="Dataset clarification required",
                ),
            )

            return update

        db_type = schema.get("db_type") or ""

        if not db_type:
            update = {
                "sql": None,
                "error": "Database type is missing from the discovered schema.",
                "steps": ["generate_sql"],
            }

            merged = {**state, **update}

            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    step="generate_sql",
                    error=update["error"],
                    message=update["error"],
                ),
            )

            return update

        sql: str | None = None

        analysis = _get_analysis_state(state)
        ctx = _format_analysis_state_context_from_state(state)

        plan = None

        clarification_plan = resolve_query_clarification(
            question,
            analysis,
        )

        follow_plan, handled = resolve_follow_up(
            question,
            analysis,
        )

        if clarification_plan is not None:
            plan = clarification_plan

        elif handled and follow_plan is not None:
            plan = follow_plan

        else:
            plan = parse_query_plan(
                self.llm,
                question,
                semantic_context,
                ctx,
            )

        has_semantic_catalog = bool(
            isinstance(semantic_context, dict)
            and (
                semantic_context.get("metrics")
                or semantic_context.get("dimensions")
            )
        )

        def _clarification_update(
            clarification: dict[str, Any],
            plan_obj: Any,
        ) -> dict[str, Any]:
            from app.agent.query_plan_validation import QueryClarification

            try:
                parsed = QueryClarification.model_validate(
                    clarification
                )
                msg = format_clarification_answer(parsed)

            except Exception:  # noqa: BLE001
                msg = str(
                    clarification.get("message")
                    or "I need clarification before running this query."
                )

            return {
                "sql": None,
                "query_plan": (
                    plan_obj.model_dump(mode="json")
                    if plan_obj is not None
                    else None
                ),
                "query_clarification": clarification,
                "answer": msg,
                "error": None,
                "steps": ["generate_sql"],
            }

        if plan is not None:
            plan = normalize_query_plan(
                plan,
                question=question,
                semantic_catalog=semantic_context,
            )

            file_backed = is_file_backed_db_type(
                str(db_type)
            )

            # ---------------------------------------------------------
            # The deterministic compiler needs a confirmed semantic
            # metric/dimension to ground an aggregation/comparison/
            # share-of-total request — without one it would have to
            # invent a query such as:
            #
            # SELECT COUNT(*) AS row_count FROM public.orders
            #
            # So skip the compiler in that case and let the
            # schema-grounded LLM fallback below attempt the query
            # directly instead, the same way file-backed datasets do.
            #
            # The same failure also shows up whenever the plan resolved
            # *no* metrics and *no* dimensions at all, regardless of its
            # intent label — a vague question ("give me insight of sales
            # data", "show me a sales trend over time") can be classified
            # as "detail" rather than "aggregation", which isn't in the
            # set above, but an empty plan means the compiler has nothing
            # to ground a query on either way. Left unguarded, it falls
            # back to the schema's first table (alphabetically, e.g. a
            # `dim_` table) via `_first_fact_table` — which, despite the
            # name, doesn't actually look for a fact table or the
            # question's keywords at all.
            # ---------------------------------------------------------
            skip_deterministic_compile = not file_backed and (
                (
                    plan.intent in {
                        "aggregation",
                        "comparison",
                        "share_of_total",
                    }
                    and not has_semantic_catalog
                )
                or (not plan.metrics and not plan.dimensions)
            )

            if skip_deterministic_compile:
                sql = None
            else:
                validation = validate_query_plan(
                    plan,
                    question=question,
                    semantic_catalog=semantic_context,
                )

                if not validation.ok and validation.clarification is not None:
                    clarification = validation.clarification.model_dump(
                        mode="json"
                    )

                    update = _clarification_update(
                        clarification,
                        plan,
                    )

                    merged = {**state, **update}

                    _emit(
                        merged,
                        SqlGeneratedEvent(
                            **_base_event_kwargs(merged),
                            sql=None,
                            message="Query clarification required",
                        ),
                    )

                    return update

                try:
                    from app.agent.join_planner import JoinAmbiguityError

                    sql = compile_query_plan(
                        plan,
                        semantic_context,
                        schema,
                        str(db_type),
                    )

                    sql = _clean_sql(sql)

                    if sql:
                        sql = _postprocess_sql(
                            sql,
                            str(db_type),
                        )

                        consistency = verify_sql_reflects_plan(
                            sql,
                            plan,
                            semantic_context,
                            db_type=str(db_type),
                        )

                        if (
                            not consistency.ok
                            and has_semantic_catalog
                        ):
                            if consistency.clarification is not None:
                                clarification = (
                                    consistency.clarification.model_dump(
                                        mode="json"
                                    )
                                )

                                return _clarification_update(
                                    clarification,
                                    plan,
                                )

                            sql = None

                except JoinAmbiguityError as exc:
                    clarification = {
                        "kind": "join_path",
                        "message": str(exc),
                        "choices": [],
                    }

                    update = _clarification_update(
                        clarification,
                        plan,
                    )

                    merged = {**state, **update}

                    _emit(
                        merged,
                        ErrorEvent(
                            **_base_event_kwargs(merged),
                            step="generate_sql",
                            error=str(exc),
                            message=str(exc),
                        ),
                    )

                    return update
                except Exception as exc:  # noqa: BLE001
                    sql = None
                    error_msg = f"Could not generate SQL: {str(exc)[:200]}"
                    logger.exception("SQL compilation failed")
                    merged = {
                        **state,
                        "sql": None,
                        "error": error_msg,
                        "steps": ["generate_sql"],
                    }
                    _emit(
                        merged,
                        ErrorEvent(
                            **_base_event_kwargs(merged),
                            step="generate_sql",
                            error=error_msg,
                            message=error_msg,
                        ),
                    )

        if plan is None and not sql:
            metric_choices = [
                str(item.get("name"))
                for item in (semantic_context.get("metrics") or [])
                if isinstance(item, dict)
                and item.get("name")
            ][:6]

            clarification = {
                "kind": "pick_metric",
                "message": (
                    "I couldn't determine which metric or dimension to use. "
                    "Confirm metrics in Semantic settings if none are listed, "
                    "or pick a confirmed metric."
                ),
                "choices": [
                    {
                        "id": str(i + 1),
                        "label": name,
                    }
                    for i, name in enumerate(metric_choices)
                ],
            }

            return _clarification_update(
                clarification,
                None,
            )

        # Fall back to direct schema-grounded LLM SQL generation when the
        # deterministic query-plan compiler cannot produce SQL — for any
        # connection type, not just file-backed datasets. This is what
        # lets a real DB connection answer questions even before any
        # semantic metric has been confirmed for it.
        if not sql:
            generated_sql, no_matching_data = generate_schema_grounded_sql(
                llm=self.llm,
                db_type=str(db_type),
                schema=schema,
                semantic_context=semantic_context,
                question=question,
                conversation_context=ctx,
            )

            if no_matching_data:
                all_names = {
                    str(t.get("table_name"))
                    for t in (schema.get("tables") or [])
                    if isinstance(t, dict) and t.get("table_name")
                }
                # Fact tables first: an alphabetical cut-off used to list
                # every dim_ table and hide fact_sales/fact_production — the
                # very tables most questions are about.
                ordered = sorted(
                    all_names,
                    key=lambda n: (not n.lower().startswith(("fact_", "f_", "fct_")), n),
                )
                table_names = ordered[:15]
                more = len(ordered) - len(table_names)
                listed = ", ".join(table_names) if table_names else "none"
                if more > 0:
                    listed += f" and {more} more"
                clarification = {
                    "kind": "invalid_plan",
                    "message": (
                        "This connection's schema doesn't appear to "
                        "contain data about that. Available tables: "
                        + listed
                        + ". Try asking about one of those, or pick a "
                        "different connection."
                    ),
                    "choices": [],
                }
                return _clarification_update(
                    clarification,
                    plan,
                )

            sql = generated_sql

        early_fallback_label: str | None = None
        if not sql:
            # The LLM produced nothing usable (e.g. no LLM_API_KEY configured,
            # so every .complete() call is a stub) — try the deterministic
            # schema-safe fallback before giving up. Without this, the one
            # mechanism built to keep answering when the LLM can't help is
            # unreachable in exactly the scenario it exists for: _ensure_valid_sql
            # below only reaches _build_fallback_choice when `sql` was non-empty
            # but invalid, never when it was empty from the start.
            fallback_choice = _build_fallback_choice(schema, question)
            if fallback_choice:
                try:
                    sql = _validate_sql(fallback_choice.sql, schema, str(db_type))
                    early_fallback_label = fallback_choice.label
                except ValueError:
                    sql = None

        if not sql:
            if has_semantic_catalog:
                clarification = {
                    "kind": "invalid_plan",
                    "message": (
                        "I couldn't compile a safe query for that request. "
                        "Try specifying the metric, dimension, or time "
                        "range more clearly."
                    ),
                    "choices": [],
                }
            else:
                clarification = {
                    "kind": "pick_metric",
                    "message": (
                        "I couldn't generate a safe query for that "
                        "request from the current schema. Try "
                        "specifying the metric, dimension, or table "
                        "more clearly, or describe your tables and columns "
                        "in the data dictionary for more precise answers."
                    ),
                    "choices": [],
                }

            return _clarification_update(
                clarification,
                plan,
            )

        sql, gen_error, fallback_label = _ensure_valid_sql(
            sql,
            schema=schema,
            db_type=str(db_type),
            semantic_context=semantic_context,
            question=question,
            llm=self.llm,
        )
        fallback_label = fallback_label or early_fallback_label
        if sql and not gen_error:
            sql = _widen_top_one(sql, question)

        if gen_error:
            update = {
                "sql": None,
                "error": gen_error,
                "steps": ["generate_sql"],
            }

            merged = {**state, **update}

            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    step="generate_sql",
                    error=update["error"],
                    message=update["error"],
                ),
            )

            return update

        update = {
            "sql": sql,
            "query_plan": (
                plan.model_dump(mode="json")
                if plan is not None
                else None
            ),
            "error": None,
            "used_fallback_sql_label": fallback_label,
            "steps": ["generate_sql"],
        }

        merged = {**state, **update}

        _emit(
            merged,
            SqlGeneratedEvent(
                **_base_event_kwargs(merged),
                sql=sql,
                message="SQL generated",
            ),
        )

        return update

    def _emit_executed(self, state: WorkflowState, update: dict[str, Any]) -> None:
        merged = {**state, **update}
        rows = list(update.get("rows") or [])
        _emit(
            merged,
            QueryExecutedEvent(
                **_base_event_kwargs(merged),
                sql=update.get("sql", state.get("sql")),
                row_count=len(rows),
                rows_preview=rows[:_ROWS_PREVIEW_LIMIT],
                message=f"Query returned {len(rows)} row(s)",
            ),
        )

class QueryExecutor:
    """Run safe SQL using tools.customer_db."""

    def __init__(self, schema_cache: SchemaCache | None = None) -> None:
        self.schema_cache = schema_cache or SchemaCache()

    def run(self, state: WorkflowState) -> dict[str, Any]:
        # last_sql on AnalysisState is context-only for follow-ups; execution always
        # uses freshly validated state["sql"] through SQLSafetyPipeline below.
        tenant_id = state.get("tenant_id") or ""
        connection_id = state.get("connection_id") or ""
        sql = state.get("sql")

        if not connection_id:
            update: dict[str, Any] = {
                "rows": [],
                "steps": ["execute_query"],
            }
            self._emit_executed(state, update)
            return update

        if not sql:
            update = {
                "rows": [],
                "steps": ["execute_query"],
            }
            self._emit_executed(state, update)
            return update

        schema = _resolve_schema(
            tenant_id,
            connection_id,
            self.schema_cache,
        )

        if schema is None:
            update = {
                "rows": [],
                "steps": ["execute_query"],
                "error": state.get("error") or _MISSING_SCHEMA_MSG,
            }
            self._emit_executed(state, update)
            return update

        semantic_context = _resolve_semantic(
            tenant_id,
            connection_id,
            SemanticCache(),
        )

        schema, semantic_context, _, dataset_clarification, dataset_error = (
            _apply_file_dataset_to_analytical_context(
                state,
                schema,
                semantic_context,
            )
        )

        if dataset_error or dataset_clarification:
            update = {
                "rows": [],
                "steps": ["execute_query"],
                "error": (
                    dataset_error
                    or "Dataset clarification required before query execution."
                ),
            }
            self._emit_executed(state, update)
            return update

        db_type = str(schema.get("db_type") or "")

        try:
            safe_sql = SQLSafetyPipeline(
                schema=schema,
                db_type=db_type,
            ).validate(sql)

            rows = run_readonly_query(
                connection_id,
                safe_sql,
                tenant_id=UUID(str(tenant_id)),
                category=QueryCategory.ANALYTICS,
                user_id=state.get("user_id"),
                conversation_id=state.get("conversation_id"),
            )

            update = {
                "rows": rows,
                "sql": safe_sql,
                "steps": ["execute_query"],
                "error": None,
            }

            self._emit_executed(state, update)
            return update

        except Exception as exc:  # noqa: BLE001
            if is_non_retryable_execution_error(exc):
                error = user_facing_execution_error(exc)

                update = {
                    "rows": [],
                    "steps": ["execute_query"],
                    "error": error,
                }

                merged = {**state, **update}

                _emit(
                    merged,
                    ErrorEvent(
                        **_base_event_kwargs(merged),
                        step="execute_query",
                        error=error,
                        detail=type(exc).__name__,
                        message=error,
                    ),
                )

                return update

            question = state.get("question") or ""
            execution_error = f"{type(exc).__name__}: {exc}"
            logger.warning("chat_sql_execution_failed error=%s", execution_error[:300])

            recovered_sql, recovered_fallback_label = _recover_sql_after_execution_error(
                sql=str(sql),
                schema=schema,
                db_type=db_type,
                semantic_context=semantic_context,
                question=question,
                llm=get_llm(),
                execution_error=execution_error,
            )

            if recovered_sql:
                try:
                    rows = run_readonly_query(
                        connection_id,
                        recovered_sql,
                        tenant_id=UUID(str(tenant_id)),
                        category=QueryCategory.ANALYTICS,
                        user_id=state.get("user_id"),
                        conversation_id=state.get("conversation_id"),
                    )

                    update = {
                        "rows": rows,
                        "sql": recovered_sql,
                        "used_fallback_sql_label": recovered_fallback_label,
                        "steps": ["execute_query"],
                        "error": None,
                    }

                    self._emit_executed(state, update)
                    return update

                except Exception as retry_exc:  # noqa: BLE001
                    if is_non_retryable_execution_error(retry_exc):
                        exc = retry_exc
                    else:
                        exc = retry_exc

            if is_non_retryable_execution_error(exc):
                error = user_facing_execution_error(exc)
            else:
                error = (
                    f"SQL execution blocked: "
                    f"{type(exc).__name__}: {exc}"
                )

            update = {
                "rows": [],
                "steps": ["execute_query"],
                "error": error,
            }

            merged = {**state, **update}

            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    step="execute_query",
                    error=error,
                    detail=type(exc).__name__,
                    message=error,
                ),
            )

            return update

    def _emit_executed(
        self,
        state: WorkflowState,
        update: dict[str, Any],
    ) -> None:
        merged = {**state, **update}
        rows = list(update.get("rows") or [])

        _emit(
            merged,
            QueryExecutedEvent(
                **_base_event_kwargs(merged),
                sql=update.get("sql", state.get("sql")),
                row_count=len(rows),
                rows_preview=rows[:_ROWS_PREVIEW_LIMIT],
                message=f"Query returned {len(rows)} row(s)",
            ),
        )
class DocumentRetrieveNode:
    """RAG retrieval node — merges chunks into workflow state."""

    def __init__(self, pipeline: RAGPipeline | None = None) -> None:
        self.pipeline = pipeline

    def run(self, state: WorkflowState) -> dict[str, Any]:
        from app.tools.rag import VectorDocumentRetriever

        tenant_id = state.get("tenant_id") or ""
        question = state.get("question") or ""
        settings = get_settings()
        filters: dict[str, Any] = {}
        analysis = state.get("analysis_state") or {}
        if isinstance(analysis, dict):
            doc_ids = analysis.get("document_ids")
            if doc_ids:
                filters["document_ids"] = doc_ids

        request = RAGRetrieveRequest(
            tenant_id=tenant_id,
            question=_retrieval_query(question, analysis),
            top_k=settings.rag_top_k,
            filters=filters,
        )

        if self.pipeline is not None:
            response = self.pipeline.retrieve(request)
        else:
            db = SessionLocal()
            try:
                response = VectorDocumentRetriever(db).retrieve(request)
            except Exception as exc:  # noqa: BLE001 — degrade when RAG store unavailable
                logger.warning(
                    "rag_retrieve_failed tenant=%s error=%s",
                    tenant_id,
                    type(exc).__name__,
                )
                response = RAGRetrieveResponse(
                    query=question,
                    chunks=[],
                    context="",
                    insufficient=True,
                )
            finally:
                db.close()

        update = response.to_state_update()
        update["steps"] = ["retrieve_docs"]
        update["rag_insufficient"] = bool(response.insufficient)

        if state.get("intent") == "rag" and not state.get("answer"):
            if response.insufficient or not response.chunks:
                update["answer"] = (
                    "I could not find relevant documents for that question. "
                    "Upload documents first, or ask about your connected database."
                )
            else:
                # Defer final narration to InsightGenerator with grounded prompt
                update["answer"] = None
        return update


def _persist_built_dashboard(
    state: WorkflowState, dashboard: dict[str, Any], connection_id: str
) -> dict[str, Any]:
    """Save a freshly built dashboard (or overwrite the conversation's one);
    returns the persisted form, or the input unchanged when it can't be saved."""
    user_id = state.get("user_id")
    if user_id and connection_id:
        try:
            db = SessionLocal()
            try:
                user = db.get(User, UUID(str(user_id)))
                if user is not None:
                    conv_id = state.get("conversation_id")
                    data_ok = [
                        w
                        for w in dashboard.get("widgets") or []
                        if isinstance(w, dict)
                        and w.get("type") not in {"ai_insight", "text"}
                    ]
                    if data_ok:
                        existing_id = None
                        conv = None
                        if conv_id:
                            conv = db.get(Conversation, UUID(str(conv_id)))
                            if conv is not None and conv.dashboard_id:
                                existing_id = conv.dashboard_id
                        if existing_id is not None:
                            persisted = update_dashboard_from_spec(
                                db,
                                dashboard_id=existing_id,
                                user=user,
                                spec=dashboard,
                            )
                        else:
                            # dashboards.conversation_id is a real FK
                            # to conversations.id. For the first
                            # message of a brand-new chat, that row
                            # hasn't been inserted yet — it's only
                            # created afterwards, once the workflow
                            # result comes back (see
                            # ConversationStateService.save). Passing
                            # a conversation_id that doesn't exist
                            # yet would violate the FK and (before
                            # this fix) get silently swallowed below,
                            # so the dashboard never got saved even
                            # though the chat reply looked like it
                            # had succeeded. Only link an existing
                            # conversation row; an unlinked dashboard
                            # still saves and shows up in the
                            # dashboard list.
                            persisted = create_dashboard_from_spec(
                                db,
                                user,
                                UUID(str(connection_id)),
                                dashboard,
                                conversation_id=(
                                    UUID(str(conv_id))
                                    if conv is not None
                                    else None
                                ),
                            )
                            if conv is not None:
                                conv.dashboard_id = UUID(str(persisted["id"]))
                                db.commit()
                        dashboard = persisted
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            # Was a bare `except: pass` — a persistence failure here
            # was invisible even though the chat reply below still
            # narrates the built dashboard as if it had saved. Log it
            # so a real failure (not just the FK case fixed above)
            # is diagnosable instead of silently disappearing.
            logger.exception(
                "Dashboard persistence failed for connection_id=%s "
                "conversation_id=%s",
                connection_id,
                state.get("conversation_id"),
            )
    return dashboard


class DashboardBuilder:
    """Build a multi-widget dashboard from the discovered schema."""

    def __init__(
        self,
        schema_cache: SchemaCache | None = None,
        semantic_cache: SemanticCache | None = None,
    ) -> None:
        self.schema_cache = schema_cache or SchemaCache()
        self.semantic_cache = semantic_cache or SemanticCache()

    def run(self, state: WorkflowState) -> dict[str, Any]:
        tenant_id = state.get("tenant_id") or ""
        connection_id = state.get("connection_id") or ""

        if wants_document_dashboard(attached_ids=state.get("attached_document_ids")):
            return self._run_documents(state)

        schema = _resolve_schema(tenant_id, connection_id, self.schema_cache)
        if schema is None:
            update: dict[str, Any] = {
                "dashboard": None,
                "rows": [],
                "error": _MISSING_SCHEMA_MSG,
                "steps": ["build_dashboard"],
            }
            return update

        db_type = str(schema.get("db_type") or "postgres")
        semantic = _resolve_semantic(
            tenant_id, connection_id, self.semantic_cache, include_suggested=True
        )
        schema, semantic, question, dataset_clarification, dataset_error = (
            _apply_file_dataset_to_analytical_context(state, schema, semantic)
        )
        focus_text = _conversation_focus_text(state)
        if dataset_error:
            return {
                "dashboard": None,
                "rows": [],
                "error": dataset_error,
                "answer": dataset_error,
                "steps": ["build_dashboard"],
            }
        if dataset_clarification is not None:
            answer = format_dataset_clarification_answer(dataset_clarification)
            return {
                "dashboard": None,
                "rows": [],
                "dataset_clarification": dataset_clarification,
                "answer": answer,
                "steps": ["build_dashboard"],
            }
        llm = get_llm()
        tenant_uuid = UUID(str(tenant_id))
        try:
            if state.get("dashboard_scope") == "whole_database":
                dashboard = build_full_database_dashboard(
                    schema,
                    connection_id=connection_id,
                    db_type=db_type,
                    semantic=semantic,
                    tenant_id=tenant_uuid,
                    llm=llm,
                )
            else:
                dashboard = build_dashboard(
                    schema,
                    connection_id=connection_id,
                    question=question,
                    focus_text=focus_text,
                    db_type=db_type,
                    semantic=semantic,
                    llm=llm,
                    tenant_id=tenant_uuid,
                )
            rows = dashboard_to_rows(dashboard)
            dashboard = _persist_built_dashboard(state, dashboard, connection_id)
            update = {
                "dashboard": dashboard,
                "rows": rows,
                "chart_suggestion": primary_chart_suggestion(dashboard),
                "error": None,
                "steps": ["build_dashboard"],
            }
        except Exception as exc:  # noqa: BLE001
            update = {
                "dashboard": None,
                "rows": [],
                "error": f"Dashboard build failed: {exc}",
                "steps": ["build_dashboard"],
            }
        return update

    def _run_documents(self, state: WorkflowState) -> dict[str, Any]:
        """Dashboard from the attached documents' own figures (never the database)."""
        tenant_id = state.get("tenant_id") or ""
        doc_ids = list(state.get("attached_document_ids") or []) or list(
            state.get("conversation_document_ids") or []
        )
        db = SessionLocal()
        try:
            text, sources = load_document_text(db, tenant_id, doc_ids)
        except Exception:  # noqa: BLE001
            logger.exception("document_dashboard_load_failed tenant=%s", tenant_id)
            text, sources = "", []
        finally:
            db.close()
        dashboard = build_document_dashboard(
            question=state.get("question") or "",
            document_text=text,
            sources=sources,
            llm=get_llm(),
        )
        built = dashboard
        dashboard = _persist_built_dashboard(state, dashboard, state.get("connection_id") or "")
        # Persisting keeps widgets/layout only; the reply still needs these.
        for key in ("build_mode", "summary", "source_documents", "fallback_reason", "dropped_figures"):
            if built.get(key) is not None:
                dashboard[key] = built[key]
        return {
            "dashboard": dashboard,
            "rows": dashboard_to_rows(dashboard),
            "chart_suggestion": primary_chart_suggestion(dashboard),
            "error": None,
            "steps": ["build_dashboard"],
        }


class DashboardEditor:
    """Edit the active dashboard via structured operations (single-dashboard guard)."""

    def __init__(
        self,
        schema_cache: SchemaCache | None = None,
        semantic_cache: SemanticCache | None = None,
    ) -> None:
        self.schema_cache = schema_cache or SchemaCache()
        self.semantic_cache = semantic_cache or SemanticCache()

    def run(self, state: WorkflowState) -> dict[str, Any]:
        tenant_id = state.get("tenant_id") or ""
        connection_id = state.get("connection_id") or ""
        question = state.get("question") or ""
        analysis = _get_analysis_state(state)

        dashboard_id = _resolve_active_dashboard_id(state, analysis)
        if dashboard_id is None:
            msg = (
                "No dashboard is open in this conversation. "
                "Say 'create a dashboard' first."
            )
            return {
                "dashboard": None,
                "rows": [],
                "error": msg,
                "dashboard_clarification": msg,
                "steps": ["edit_dashboard"],
            }

        user_id = state.get("user_id")
        if not user_id:
            return {
                "dashboard": None,
                "rows": [],
                "error": "Authentication required to edit dashboards.",
                "steps": ["edit_dashboard"],
            }

        tenant_uuid = UUID(str(tenant_id))
        request_connection_uuid: UUID | None = None
        if connection_id:
            try:
                request_connection_uuid = UUID(str(connection_id))
            except ValueError:
                request_connection_uuid = None

        pending_op: PendingDashboardOp | None = None
        if analysis.dashboard.pending_operation:
            try:
                pending_op = PendingDashboardOp.model_validate(
                    analysis.dashboard.pending_operation
                )
            except Exception:  # noqa: BLE001
                pending_op = None

        try:
            db = SessionLocal()
            try:
                user = db.get(User, UUID(str(user_id)))
                if user is None:
                    raise ValueError("User not found")
                existing = get_dashboard(db, dashboard_id, user)
                if existing is None:
                    raise ValueError("Dashboard not found or access denied")
                execution_connection_id = resolve_dashboard_execution_connection(
                    db,
                    dashboard=existing,
                    request_connection_id=request_connection_uuid,
                    tenant_id=tenant_uuid,
                )
            finally:
                db.close()
        except DashboardConnectionMismatchError as exc:
            err = str(exc)
            merged = {**state, "error": err}
            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    error=err,
                    message=err,
                ),
            )
            return {
                "dashboard": None,
                "rows": [],
                "error": err,
                "error_code": DashboardConnectionMismatchError.code,
                "steps": ["edit_dashboard"],
            }
        except Exception as exc:  # noqa: BLE001
            err = f"Could not load dashboard: {exc}"
            merged = {**state, "error": err}
            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    error=err,
                    message=err,
                ),
            )
            return {
                "dashboard": None,
                "rows": [],
                "error": err,
                "steps": ["edit_dashboard"],
            }

        execution_connection_str = str(execution_connection_id)
        schema = _resolve_schema(tenant_id, execution_connection_str, self.schema_cache)
        if schema is None:
            return {
                "dashboard": None,
                "rows": [],
                "error": _MISSING_SCHEMA_MSG,
                "steps": ["edit_dashboard"],
            }

        db_type = str(schema.get("db_type") or "postgres")
        semantic = _resolve_semantic(
            tenant_id, execution_connection_str, self.semantic_cache, include_suggested=True
        )

        prev_conn = state.get("connection_id")
        state["connection_id"] = execution_connection_str
        schema, semantic, _, dataset_clarification, dataset_error = (
            _apply_file_dataset_to_analytical_context(state, schema, semantic)
        )
        if prev_conn:
            state["connection_id"] = prev_conn
        if dataset_error:
            return {
                "dashboard": None,
                "rows": [],
                "error": dataset_error,
                "steps": ["edit_dashboard"],
            }
        if dataset_clarification is not None:
            return {
                "dashboard": None,
                "rows": [],
                "dataset_clarification": dataset_clarification,
                "answer": format_dataset_clarification_answer(dataset_clarification),
                "steps": ["edit_dashboard"],
            }

        baseline = execute_dashboard(
            existing,
            connection_id=execution_connection_str,
            schema=schema,
            db_type=db_type,
            tenant_id=tenant_uuid,
            semantic=semantic,
        )
        baseline["id"] = str(dashboard_id)
        catalog_dicts = catalog_to_dicts(build_widget_catalog(baseline, semantic))

        result = run_dashboard_edit_pipeline(
            instruction=question,
            dashboard=baseline,
            dashboard_id=dashboard_id,
            semantic_catalog=semantic,
            schema=schema,
            db_type=db_type,
            connection_id=execution_connection_str,
            tenant_id=tenant_uuid,
            llm=get_llm(),
            active_widget_id=analysis.dashboard.active_widget_id,
            pending_operation=pending_op,
        )

        if not result.success:
            if result.clarification:
                msg = result.clarification.message
                if result.clarification.choices:
                    choice_lines = [
                        f"{c.id}. {c.label}" for c in result.clarification.choices
                    ]
                    msg = f"{msg}\n" + "\n".join(choice_lines)
                pending_dump = (
                    result.pending_operation.model_dump(mode="json")
                    if result.pending_operation
                    else None
                )
                return {
                    "dashboard": baseline,
                    "rows": dashboard_to_rows(baseline),
                    "chart_suggestion": primary_chart_suggestion(baseline),
                    "dashboard_edit_notes": [msg],
                    "dashboard_clarification": msg,
                    "dashboard_pending_operation": pending_dump,
                    "dashboard_widget_catalog": catalog_dicts,
                    "error": None,
                    "steps": ["edit_dashboard"],
                }

            err = result.error or "Dashboard edit failed."
            merged = {**state, "error": err}
            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    error=err,
                    message=err,
                ),
            )
            return {
                "dashboard": baseline,
                "rows": dashboard_to_rows(baseline),
                "dashboard_edit_notes": [err],
                "error": err,
                "steps": ["edit_dashboard"],
            }

        dashboard = result.dashboard or baseline
        edit_notes = result.edit_notes

        try:
            db = SessionLocal()
            try:
                user = db.get(User, UUID(str(user_id)))
                if user is not None:
                    dashboard = update_dashboard_from_spec(
                        db,
                        dashboard_id=dashboard_id,
                        user=user,
                        spec=dashboard,
                    )
                    executed = execute_dashboard(
                        dashboard,
                        connection_id=execution_connection_str,
                        schema=schema,
                        db_type=db_type,
                        tenant_id=tenant_uuid,
                        semantic=semantic,
                    )
                    dashboard = update_dashboard_from_spec(
                        db,
                        dashboard_id=dashboard_id,
                        user=user,
                        spec=executed,
                        save_version=False,
                    )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            err = f"Dashboard edit failed to save: {exc}"
            merged = {**state, "error": err}
            _emit(
                merged,
                ErrorEvent(
                    **_base_event_kwargs(merged),
                    error=err,
                    message=err,
                ),
            )
            return {
                "dashboard": baseline,
                "rows": dashboard_to_rows(baseline),
                "dashboard_edit_notes": [err],
                "error": err,
                "steps": ["edit_dashboard"],
            }

        new_catalog = catalog_to_dicts(
            build_widget_catalog(dashboard, semantic)
        )
        rows = dashboard_to_rows(dashboard)
        update: dict[str, Any] = {
            "dashboard": dashboard,
            "rows": rows,
            "chart_suggestion": primary_chart_suggestion(dashboard),
            "dashboard_edit_notes": edit_notes,
            "dashboard_edit_applied": True,
            "dashboard_pending_operation": None,
            "dashboard_widget_catalog": new_catalog,
            "dashboard_modified_widget_ids": result.modified_widget_ids,
            "error": None,
            "steps": ["edit_dashboard"],
        }
        merged = {**state, **update}
        _emit(
            merged,
            DashboardUpdatedEvent(
                **_base_event_kwargs(merged),
                dashboard_id=str(dashboard_id),
                dashboard=dashboard,
                edit_notes=edit_notes,
                message="Dashboard updated",
            ),
        )
        return update


# Whole underscore-separated tokens, so "downtime_hours" or "runtime" are
# not mistaken for a time axis.
_TEMPORAL_COLUMN_RE = re.compile(
    r"(?:^|_)(date|day|week|month|quarter|year|period|dt|timestamp|time|fiscal_year|fiscal_quarter|fiscal_month)(?:_|$)|_at$",
    re.IGNORECASE,
)


class ChartRecommender:
    """Suggest a chart type from dashboard or result rows."""

    def run(self, state: WorkflowState) -> dict[str, Any]:
        dashboard = state.get("dashboard")
        if isinstance(dashboard, dict):
            suggestion = primary_chart_suggestion(dashboard)
            if suggestion:
                return {
                    "chart_suggestion": suggestion,
                    "steps": ["recommend_chart"],
                }

        rows = state.get("rows") or []
        if len(rows) < 2:
            return {"chart_suggestion": None, "steps": ["recommend_chart"]}

        from app.agent.chat_insight_facts import _is_identifier_like_key

        keys = list(rows[0].keys()) if rows else []
        numeric_keys: list[str] = []
        text_keys: list[str] = []
        for key in keys:
            sample = [r.get(key) for r in rows[:10]]
            nums = sum(1 for v in sample if isinstance(v, (int, float)))
            if nums >= max(1, len(sample) // 2):
                numeric_keys.append(key)
            else:
                text_keys.append(key)

        # Identifier-like numeric columns (customer_id, product_key, ...)
        # are not real measures and must never drive chart-type selection.
        # Period columns (year / month / date...) are the time axis, never
        # measures — even when stored as numbers.
        temporal_keys = [k for k in keys if _TEMPORAL_COLUMN_RE.search(str(k))]
        measure_keys = [
            k for k in numeric_keys if not _is_identifier_like_key(k) and k not in temporal_keys
        ]
        label_keys = [k for k in text_keys if k not in temporal_keys]

        from app.agent.measure_kind import ADDITIVE_KINDS, resolve_column_kinds

        kinds = resolve_column_kinds(measure_keys, sql=state.get("sql"))

        if measure_keys and temporal_keys:
            return {"chart_suggestion": "line", "steps": ["recommend_chart"]}

        if measure_keys and label_keys:
            if len(measure_keys) >= 2:
                # Multiple measures broken down by the same label — compare
                # them (the UI splits different scales into small multiples).
                return {"chart_suggestion": "combo", "steps": ["recommend_chart"]}
            distinct_labels = len({r.get(label_keys[0]) for r in rows})
            values = [r.get(measure_keys[0]) for r in rows]
            is_additive = kinds.get(measure_keys[0], "amount") in ADDITIVE_KINDS
            non_negative = all(isinstance(v, (int, float)) and v >= 0 for v in values)
            if 2 <= distinct_labels <= 6 and len(label_keys) == 1 and is_additive and non_negative:
                # Few parts of an additive whole — a share-of-total donut.
                # Percentages, ratios and averages are never pies: their
                # slices would not add up to anything.
                return {"chart_suggestion": "pie", "steps": ["recommend_chart"]}
            return {"chart_suggestion": "bar", "steps": ["recommend_chart"]}

        if len(measure_keys) >= 2:
            # Two real measures and no categorical label — a correlation
            # between them is more useful than an arbitrary bar/line pick.
            return {"chart_suggestion": "scatter", "steps": ["recommend_chart"]}

        return {"chart_suggestion": None, "steps": ["recommend_chart"]}


class InsightGenerator:
    """Explain the result in plain English, merging SQL + RAG context."""

    def run(self, state: WorkflowState) -> dict[str, Any]:
        intent = state.get("intent") or "sql"
        question = state.get("question") or ""
        error = state.get("error")

        chat_grounding: dict[str, Any] | None = None

        if intent == "greeting":
            answer = "Hello! Ask me a question about your data."
        elif intent == "schema":
            answer = state.get("answer") or _MISSING_SCHEMA_MSG
        elif intent == "history":
            answer = state.get("answer") or "I couldn't read your chat history just now."
        elif intent == "investigate":
            # Investigator already synthesized the final report; this node
            # is just the finalization/grounding-metadata step every intent
            # passes through, same as the schema branch above.
            answer = state.get("answer") or (
                "I could not complete an investigation for this question."
            )
        elif intent == "rag":
            rag_context = state.get("rag_context") or ""
            if state.get("rag_insufficient") or not (state.get("rag_chunks") or []):
                answer = state.get("answer") or (
                    "I could not find relevant documents for that question. "
                    "Upload documents first, or ask about your connected database."
                )
            else:
                answer = _narrate_rag_answer(
                    question=question,
                    rag_context=rag_context,
                    llm=get_llm(),
                    conversation_context=_format_analysis_state_context_from_state(state),
                )
        elif state.get("query_clarification"):
            answer = str(state.get("answer") or "").strip() or (
                "I need clarification before running this query."
            )
        elif state.get("dataset_clarification"):
            answer = str(state.get("answer") or "").strip() or (
                "I need to know which dataset to use before running this query."
            )
        elif error:
            answer = f"I couldn't complete that request. {error}"
        elif intent in {"dashboard", "edit_dashboard"}:
            clarification = state.get("dashboard_clarification")
            dashboard = state.get("dashboard")
            if clarification:
                answer = str(clarification)
            elif not isinstance(dashboard, dict) or not dashboard.get("widgets"):
                answer = error or (
                    str(dashboard.get("fallback_reason") or "") if isinstance(dashboard, dict) else ""
                ) or "I could not build a dashboard for this connection."
            else:
                data_ok = [
                    w
                    for w in dashboard.get("widgets") or []
                    if isinstance(w, dict) and w.get("type") not in {"ai_insight", "text"}
                ]
                if not data_ok:
                    reason = str(dashboard.get("fallback_reason") or "").strip() or (
                        "There is not enough informative structure in this dataset "
                        "to build a reliable dashboard."
                    )
                    answer = f"## {dashboard.get('title') or 'Dashboard'}\n\n{reason}"
                else:
                    lines = [f"## {dashboard.get('title') or 'Dashboard'}", ""]
                    build_mode = dashboard.get("build_mode") or ""
                    if build_mode == "document":
                        names = ", ".join(
                            str(d.get("filename") or "")
                            for d in dashboard.get("source_documents") or []
                            if isinstance(d, dict)
                        )
                        lines.append(f"*Built from {names or 'your attached document'}.*")
                        lines.append("")
                        if dashboard.get("summary"):
                            lines.append(str(dashboard["summary"]))
                            lines.append("")
                    elif build_mode == "semantic_llm":
                        lines.append(
                            "*Built from your semantic layer and discovered schema.*"
                        )
                        lines.append("")
                    elif build_mode == "llm":
                        lines.append("*Built from discovered schema via analyst planner.*")
                        lines.append("")
                    elif build_mode == "heuristic_no_llm":
                        lines.append(
                            "*Tip: define metrics/dimensions in Semantic settings and "
                            "configure LLM_API_KEY for richer dashboards on messy schemas.*"
                        )
                        lines.append("")
                    # This list is flat, unlike the board's section groups, so
                    # a title shared by two sections (e.g. "Rejected %" from
                    # production and from procurement) gets its section named.
                    sections_by_title: dict[str, set[str]] = {}
                    for widget in dashboard.get("widgets") or []:
                        if isinstance(widget, dict) and widget.get("title"):
                            sections_by_title.setdefault(
                                str(widget["title"]).strip().lower(), set()
                            ).add(str(widget.get("section") or ""))
                    for widget in dashboard.get("widgets") or []:
                        if not isinstance(widget, dict):
                            continue
                        if widget.get("type") in {"ai_insight", "text"}:
                            # Not row-based — counting `len(rows)` here would
                            # print "0 data point(s)" even when it holds a
                            # real generated insight (its content lives in
                            # config, not rows). It already gets its own
                            # "See the AI Insights widget..." pointer below.
                            continue
                        title = widget.get("title") or "Widget"
                        section = str(widget.get("section") or "")
                        if section and len(sections_by_title.get(str(title).strip().lower(), ())) > 1:
                            title = f"{title} ({section})"
                        if widget.get("type") == "kpi":
                            val = widget.get("value")
                            if val is None and widget.get("rows"):
                                val = (widget["rows"][0] or {}).get("value")
                            lines.append(f"- **{title}:** {_format_kpi_value(val, title)}")
                        elif widget.get("error"):
                            lines.append(f"- **{title}:** unavailable ({widget['error']})")
                        else:
                            count = len(widget.get("rows") or [])
                            lines.append(f"- **{title}:** {count} data point(s)")
                    lines.append("")
                    edit_notes = state.get("dashboard_edit_notes") or []
                    if edit_notes:
                        lines.append(" ".join(str(n) for n in edit_notes))
                        lines.append("")
                    if build_mode == "document":
                        if dashboard.get("dropped_figures"):
                            lines.append(
                                f"*{dashboard['dropped_figures']} figure(s) left out because "
                                "they could not be verified against the document text.*"
                            )
                    else:
                        lines.append(
                            "You can say things like *add trend chart*, *remove KPI*, "
                            "or *rename dashboard to 'Sales Overview'* to refine this board."
                        )
                    if any(
                        isinstance(w, dict) and w.get("type") == "ai_insight"
                        for w in dashboard.get("widgets") or []
                    ):
                        lines.append("")
                        lines.append(
                            "See the **AI Insights** widget on the dashboard for automated analysis."
                        )
                    answer = "\n".join(lines)
        elif intent in {"sql", "hybrid"} and not state.get("sql"):
            answer = _MISSING_SCHEMA_MSG
        elif intent in {"sql", "hybrid"}:
            from app.agent.chat_insight_facts import extract_chat_insight_facts
            from app.agent.chat_insight_narrator import narrate_chat_insights
            from app.agent.insight_validation import GroundingOutcome

            raw_plan = state.get("query_plan")
            query_plan = raw_plan if isinstance(raw_plan, dict) else None
            if raw_plan is not None and hasattr(raw_plan, "model_dump"):
                query_plan = raw_plan.model_dump(mode="json")

            analysis = state.get("analysis_state") or {}
            dash_filters: list[dict[str, Any]] = []
            if isinstance(analysis, dict):
                dash = (analysis.get("dashboard") or {}) if isinstance(analysis.get("dashboard"), dict) else {}
                dash_filters = list(dash.get("filters") or [])

            # The semantic catalog tells the facts builder which result
            # columns are percentages/ratios (never summed) — best effort:
            # the SQL structure itself is still classified without it.
            try:
                semantic_catalog = _confirmed_semantic_catalog(
                    _resolve_semantic(
                        str(state.get("tenant_id") or ""),
                        str(state.get("connection_id") or ""),
                        SemanticCache(),
                    )
                )
            except Exception:  # noqa: BLE001
                semantic_catalog = None

            facts = extract_chat_insight_facts(
                list(state.get("rows") or []),
                question=question,
                sql=state.get("sql"),
                query_plan=query_plan,
                tenant_id=state.get("tenant_id"),
                connection_id=state.get("connection_id"),
                filters=dash_filters,
                semantic_catalog=semantic_catalog,
            )
            llm = fast_llm(get_llm())
            answer, narrator_mode, grounding_outcome = narrate_chat_insights(
                facts,
                llm,
                question=question,
            )
            grounding_meta = {
                "status": grounding_outcome.value,
                "narrator": narrator_mode,
                "facts_hash": facts.scope_key[:16] if facts.scope_key else "",
                "column_kinds": dict(facts.column_kinds),
            }
            if intent == "hybrid":
                rag_note = str(state.get("rag_context") or "").strip()
                if state.get("rag_insufficient") or not rag_note:
                    answer = (
                        f"{answer}\n\n_Document context was unavailable for this answer._"
                    )
                elif grounding_outcome != GroundingOutcome.INSUFFICIENT_DATA and (
                    not state.get("conversation_document_ids") or _documents_relevant(state)
                ):
                    answer = (
                        f"{answer}\n\n### Related document context\n{rag_note}\n\n"
                        "_Numeric figures above come from the database query, "
                        "not from documents._"
                    )
            fallback_label = state.get("used_fallback_sql_label")
            if fallback_label:
                # `_build_fallback_choice` guessed a table/column
                # heuristically after the LLM's own SQL failed validation or
                # execution — name what it actually picked in plain words
                # (no SQL) so the user can spot a wrong guess by reading the
                # label, not by reading a query.
                answer = (
                    f"{answer}\n\n_This wasn't a confident match for your "
                    f"question — I used **{fallback_label}**, my closest "
                    "guess from the available data. If that's not what you "
                    "meant, try naming the exact field or table you want._"
                )
            chat_grounding = grounding_meta

        # Files attached to this message: the answer is about them. The
        # hybrid path above narrates the database and appends excerpts,
        # which answered "which supplier is on the watchlist?" with "no such
        # data" while the answer sat in the attached sheet.
        clarification = state.get("query_clarification")
        # "The database has no data about that" is not a question for the
        # user when the attached documents may answer it; a clarification
        # offering real choices still wins.
        no_matching_data = (
            isinstance(clarification, dict)
            and clarification.get("kind") == "invalid_plan"
            and not clarification.get("choices")
        )
        cleared_clarification = False
        if (
            intent == "hybrid"
            and (state.get("attached_document_ids") or state.get("conversation_document_ids"))
            and _documents_relevant(state)
            and (state.get("rag_chunks") or [])
            and not state.get("rag_insufficient")
            and (not clarification or no_matching_data)
            and not state.get("dataset_clarification")
        ):
            combined = _narrate_attached_answer(
                question=question,
                rag_context=str(state.get("rag_context") or ""),
                database_answer=(
                    "(the database has no data about this)" if no_matching_data else str(answer or "")
                ),
                llm=get_llm(),
                conversation_context=_format_analysis_state_context_from_state(state),
            )
            if combined:
                answer = combined
                cleared_clarification = no_matching_data

        update: dict[str, Any] = {"answer": answer, "steps": ["generate_insight"]}
        if cleared_clarification:
            update["query_clarification"] = None
        if chat_grounding is not None:
            update["chat_grounding"] = chat_grounding
        merged = {**state, **update}
        _emit(
            merged,
            InsightReadyEvent(
                **_base_event_kwargs(merged),
                answer=answer,
                sql=state.get("sql"),
                chart_suggestion=state.get("chart_suggestion"),
                intent=intent,
                message="Insight ready",
            ),
        )
        return update


def _route_after_intent(
    state: WorkflowState,
) -> Literal[
    "generate_sql",
    "retrieve_docs",
    "describe_schema",
    "generate_insight",
    "build_dashboard",
    "edit_dashboard",
    "investigate",
    "answer_history",
]:
    intent = state.get("intent") or "sql"
    if intent == "greeting":
        return "generate_insight"
    if intent == "schema":
        return "describe_schema"
    if intent == "rag":
        return "retrieve_docs"
    if intent == "dashboard":
        return "build_dashboard"
    if intent == "edit_dashboard":
        return "edit_dashboard"
    if intent == "investigate":
        return "investigate"
    if intent == "history":
        return "answer_history"
    return "generate_sql"


def _route_after_execute(
    state: WorkflowState,
) -> Literal["retrieve_docs", "recommend_chart"]:
    if state.get("intent") == "hybrid":
        return "retrieve_docs"
    return "recommend_chart"


def _route_after_retrieve(
    state: WorkflowState,
) -> Literal["recommend_chart", "generate_insight"]:
    if state.get("intent") == "hybrid":
        return "recommend_chart"
    return "generate_insight"


def _build_graph():
    classifier = IntentClassifier()
    sql_generator = SQLGenerator()
    query_executor = QueryExecutor()
    schema_describer = SchemaDescriber()
    document_retrieve = DocumentRetrieveNode()
    dashboard_builder = DashboardBuilder()
    dashboard_editor = DashboardEditor(semantic_cache=SemanticCache())
    chart_recommender = ChartRecommender()
    insight_generator = InsightGenerator()
    investigator = Investigator()

    graph = StateGraph(WorkflowState)
    graph.add_node("classify_intent", classifier.run)
    graph.add_node("generate_sql", sql_generator.run)
    graph.add_node("execute_query", query_executor.run)
    graph.add_node("describe_schema", schema_describer.run)
    graph.add_node("retrieve_docs", document_retrieve.run)
    graph.add_node("build_dashboard", dashboard_builder.run)
    graph.add_node("edit_dashboard", dashboard_editor.run)
    graph.add_node("recommend_chart", chart_recommender.run)
    graph.add_node("generate_insight", insight_generator.run)
    graph.add_node("investigate", investigator.run)
    graph.add_node("answer_history", HistoryAnswerer().run)

    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges(
        "classify_intent",
        _route_after_intent,
        {
            "generate_sql": "generate_sql",
            "retrieve_docs": "retrieve_docs",
            "describe_schema": "describe_schema",
            "generate_insight": "generate_insight",
            "build_dashboard": "build_dashboard",
            "edit_dashboard": "edit_dashboard",
            "investigate": "investigate",
            "answer_history": "answer_history",
        },
    )
    graph.add_edge("answer_history", "generate_insight")
    graph.add_edge("generate_sql", "execute_query")
    graph.add_edge("describe_schema", "generate_insight")
    graph.add_edge("build_dashboard", "recommend_chart")
    graph.add_edge("edit_dashboard", "recommend_chart")
    graph.add_edge("investigate", "generate_insight")
    graph.add_conditional_edges(
        "execute_query",
        _route_after_execute,
        {
            "retrieve_docs": "retrieve_docs",
            "recommend_chart": "recommend_chart",
        },
    )
    graph.add_conditional_edges(
        "retrieve_docs",
        _route_after_retrieve,
        {
            "recommend_chart": "recommend_chart",
            "generate_insight": "generate_insight",
        },
    )
    graph.add_edge("recommend_chart", "generate_insight")
    graph.add_edge("generate_insight", END)

    return graph.compile()


class BIWorkflow:
    """Wire stages with LangGraph and run the compiled graph."""

    def __init__(self) -> None:
        self._graph = _build_graph()

    def _run_supervised(
        self,
        question: str,
        initial: WorkflowState,
        *,
        source: str,
        available: dict[str, str],
        attached_now: list[str],
    ) -> SupervisorResult | None:
        """Let the supervisor agent orchestrate specialists; None falls back to
        the pipeline (no API key, or the supervisor call failed)."""
        llm = get_llm()
        if getattr(llm, "client", None) is None:
            return None
        tenant_id = initial.get("tenant_id") or ""
        connection_id = initial.get("connection_id") or ""
        in_scope = list(initial.get("attached_document_ids") or [])
        chat_ids = list(initial.get("conversation_document_ids") or [])

        files: list[FileInfo] = []
        total_files = 0
        if source == "documents":
            files = [FileInfo(id=d, filename=available.get(d, ""), in_chat=True, attached_now=d in attached_now) for d in in_scope]
        elif source == "auto":
            pinned = [*in_scope, *[d for d in chat_ids if d not in in_scope]]
            try:
                db = SessionLocal()
                try:
                    found = find_candidate_documents(
                        db,
                        tenant_id=tenant_id,
                        question=question,
                        chat_documents={d: available.get(d, "") for d in pinned},
                    )
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 — still route between the other sources
                logger.warning("supervisor_file_candidates_failed", exc_info=True)
                found = []
            # Files attached now / in the chat first, then the best other matches.
            order = {d: i for i, d in enumerate(pinned)}
            found.sort(key=lambda c: (order.get(c.id, len(order)), -c.score))
            for cand in found:
                available.setdefault(cand.id, cand.filename)
                files.append(
                    FileInfo(
                        id=cand.id,
                        filename=cand.filename,
                        snippet=cand.snippet,
                        in_chat=cand.id in chat_ids or cand.id in in_scope,
                        attached_now=cand.id in attached_now,
                    )
                )
            # Every other ready file too (names only): a question whose wording
            # doesn't match a file's passages ("what is machine learning" vs.
            # ML notes) or that is about the files themselves ("how many files
            # have I uploaded") must still see them.
            listed, total_files = _tenant_ready_documents(tenant_id, MAX_SUPERVISOR_FILES)
            dates = {fid: day for fid, _, day in listed}
            for file in files:
                file.uploaded_at = dates.get(file.id, "")
            seen = {f.id for f in files}
            for fid, filename, day in listed:
                if fid not in seen and len(files) < MAX_SUPERVISOR_FILES:
                    available.setdefault(fid, filename)
                    files.append(FileInfo(id=fid, filename=filename, uploaded_at=day))
                    seen.add(fid)

        databases: list[DatabaseInfo] = []
        if source != "documents":
            try:
                databases = _tenant_databases(tenant_id, connection_id)
            except Exception:  # noqa: BLE001 — fall back to the selected one
                logger.warning("supervisor_databases_failed", exc_info=True)
            if source == "database":
                # @database pins the chat to the selected connection.
                databases = [d for d in databases if d.selected]
            if not databases and connection_id:
                schema = _resolve_schema(tenant_id, connection_id, SchemaCache())
                databases = [
                    DatabaseInfo(
                        id=connection_id,
                        name="selected database",
                        tables=_schema_table_names(schema) if schema else [],
                        selected=True,
                    )
                ]
        analysis = initial.get("analysis_state") or {}
        try:
            context = format_analysis_state_context(AnalysisState.model_validate(analysis))
        except Exception:  # noqa: BLE001
            context = "None."
        has_dashboard = bool((analysis.get("dashboard") or {}).get("dashboard_id"))

        def run_specialist(intent: str, q: str, overrides: dict[str, Any]) -> dict[str, Any]:
            state: WorkflowState = {**initial, "question": q, "forced_intent": intent, "steps": []}
            specialist_analysis = dict(analysis)
            ids = overrides.get("document_ids")
            if ids is not None:
                state["attached_document_ids"] = list(ids)
                state["conversation_document_ids"] = []
                specialist_analysis.pop("document_ids", None)
                if ids:
                    specialist_analysis["document_ids"] = list(ids)
            if overrides.get("dashboard_scope"):
                state["dashboard_scope"] = overrides["dashboard_scope"]
            other_db = overrides.get("connection_id")
            if other_db and other_db != connection_id:
                state["connection_id"] = other_db
                for key in _CONNECTION_BOUND_STATE:
                    specialist_analysis.pop(key, None)
                specialist_analysis["connection_id"] = other_db
            state["analysis_state"] = specialist_analysis
            return dict(self._graph.invoke(state))

        try:
            return run_supervisor(
                question,
                llm=llm,
                run_specialist=run_specialist,
                databases=databases,
                files=files,
                total_files=max(total_files, len(files)),
                context=context,
                source=source,
                has_dashboard=has_dashboard,
            )
        except Exception:  # noqa: BLE001 — never lose the turn; use the pipeline
            logger.warning("supervisor_failed", exc_info=True)
            return None

    def run(
        self,
        question: str,
        *,
        tenant_id: UUID | str | None = None,
        user_id: UUID | str | None = None,
        connection_id: str | None = None,
        conversation_id: UUID | str | None = None,
        run_id: str | None = None,
        document_ids: list[UUID] | list[str] | None = None,
        source_scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        analysis_state_dict: dict[str, Any] = {}
        conv_id = str(conversation_id) if conversation_id is not None else None
        tenant_str = str(tenant_id) if tenant_id is not None else ""
        user_str = str(user_id) if user_id is not None else ""
        if conv_id and tenant_str and user_str:
            try:
                db = SessionLocal()
                try:
                    user = db.get(User, UUID(str(user_str)))
                    if user is not None:
                        loaded = ConversationStateService().load(
                            db,
                            conversation_id=UUID(str(conv_id)),
                            user=user,
                        )
                        analysis_state_dict = loaded.model_dump(mode="json")
                        analysis_state_dict.update(
                            memory_service.recall(
                                db,
                                user=user,
                                conversation_id=UUID(str(conv_id)),
                                question=question,
                            )
                        )
                finally:
                    db.close()
            except Exception:  # noqa: BLE001
                logger.warning("conversation_state_load_failed", exc_info=True)

        attached_ids = [str(d) for d in document_ids or []]
        earlier_ids = [
            str(d.get("id"))
            for d in analysis_state_dict.get("documents") or []
            if isinstance(d, dict) and d.get("id") and str(d.get("id")) not in attached_ids
        ]
        # Only files that still exist and finished processing are searched.
        available = _ready_documents(tenant_str, [*attached_ids, *earlier_ids])
        attached_ids = [d for d in attached_ids if d in available]
        earlier_ids = [d for d in earlier_ids if d in available]
        # Files attached now join the chat whatever the scope says.
        new_documents = list(attached_ids)

        scope, explicit_scope = _resolve_source_scope(source_scope, analysis_state_dict)
        source = scope.source
        if source == "documents":
            wanted = {str(d) for d in scope.document_ids}
            in_scope = [
                d for d in [*attached_ids, *earlier_ids]
                if not wanted or d in wanted or d in new_documents
            ]
            if in_scope:
                # The chosen files count as attached to this message, so the
                # usual "answer from the attachment" handling applies.
                attached_ids, earlier_ids = in_scope, []
            else:
                source = "auto"  # the chosen files are gone or not ready yet
        elif source == "database":
            attached_ids, earlier_ids = [], []

        supervised = _use_supervisor(analysis_state_dict)
        route: SourceDecision | None = None
        if (
            not supervised
            and source == "auto"
            and not attached_ids
        ):
            # Nothing picked and nothing attached now: let the router weigh the
            # database against every uploaded file, not just this chat's.
            route = _route_auto_source(
                question,
                tenant_id=tenant_str,
                connection_id=str(connection_id or ""),
                chat_documents={d: available.get(d, "") for d in earlier_ids},
                analysis_state=analysis_state_dict,
            )
        if route is not None:
            for doc_id, filename in route.filenames.items():
                available.setdefault(doc_id, filename)
            if route.decision.source == "database":
                attached_ids, earlier_ids = [], []
            else:
                # Chosen files count as attached: "documents" answers from them,
                # "both" runs the database too and lets the files lead.
                attached_ids = list(route.decision.document_ids)
                earlier_ids = []
                if route.decision.source == "documents":
                    source = "documents"
                new_documents.extend(
                    d for d in attached_ids if d not in new_documents and d not in route.in_chat
                )

        analysis_state_dict = dict(analysis_state_dict)
        analysis_state_dict.pop("document_ids", None)
        if attached_ids or earlier_ids:
            analysis_state_dict["document_ids"] = [*attached_ids, *earlier_ids]

        initial: WorkflowState = {
            "question": question,
            "tenant_id": tenant_str,
            "user_id": user_str,
            "connection_id": str(connection_id) if connection_id is not None else "",
            "conversation_id": conv_id,
            "run_id": run_id or str(uuid4()),
            "analysis_state": analysis_state_dict,
            "query_plan": None,
            "sql": None,
            "rows": [],
            "rag_query": None,
            "rag_chunks": [],
            "rag_context": None,
            "rag_insufficient": None,
            "chart_suggestion": None,
            "dashboard": None,
            "answer": "",
            "error": None,
            "steps": [],
            "attached_document_ids": attached_ids,
            "conversation_document_ids": earlier_ids,
            "source_scope": source,
        }
        outcome: SupervisorResult | None = None
        if supervised:
            outcome = self._run_supervised(
                question,
                initial,
                source=source,
                available=available,
                attached_now=list(new_documents),
            )
        if outcome is not None and outcome.runs:
            final = dict(outcome.primary or {})
            if outcome.answer:
                final["answer"] = outcome.answer
            final["question"] = question
            final["steps"] = ["supervisor", *[s for r in outcome.runs for s in r.get("steps") or []]]
            # Files an agent read that weren't in the chat yet join it.
            for run in outcome.runs:
                for doc_id in run.get("attached_document_ids") or []:
                    if run.get("intent") in {"rag", "dashboard"} and doc_id not in new_documents and doc_id not in earlier_ids and doc_id in available:
                        new_documents.append(doc_id)
        elif outcome is not None and outcome.answer:
            # Answered directly (greeting, "what can you do?").
            final = {**initial, "intent": "greeting", "answer": outcome.answer, "steps": ["supervisor"]}
        else:
            final = self._graph.invoke(initial)
        raw_plan = final.get("query_plan")
        if raw_plan is not None and hasattr(raw_plan, "model_dump"):
            raw_plan = raw_plan.model_dump(mode="json")
        result = {
            "answer": final.get("answer", ""),
            "sql": final.get("sql"),
            "rows": list(final.get("rows") or []),
            "dashboard": final.get("dashboard"),
            "rag_chunks": list(final.get("rag_chunks") or []),
            "rag_context": final.get("rag_context"),
            "chart_suggestion": final.get("chart_suggestion"),
            "steps": list(final.get("steps") or []),
            "error": final.get("error"),
            "question": final.get("question", question),
            "intent": final.get("intent"),
            "query_plan": raw_plan,
            "run_id": final.get("run_id"),
            "dashboard_pending_operation": final.get("dashboard_pending_operation"),
            "dashboard_widget_catalog": final.get("dashboard_widget_catalog"),
            "dashboard_edit_applied": final.get("dashboard_edit_applied"),
            "dashboard_modified_widget_ids": final.get("dashboard_modified_widget_ids"),
            "dashboard_edit_notes": final.get("dashboard_edit_notes"),
            "dashboard_clarification": final.get("dashboard_clarification"),
            "query_clarification": final.get("query_clarification"),
            "dataset_clarification": final.get("dataset_clarification"),
            "dataset_id": final.get("dataset_id"),
            "dataset_display_name": final.get("dataset_display_name"),
            "error_code": final.get("error_code"),
            "chat_grounding": final.get("chat_grounding"),
            "attached_documents": [
                {"id": d, "filename": available[d]} for d in new_documents
            ],
            "answered_from": (
                _merge_answered_from([_answered_from(r) for r in outcome.runs])
                if outcome is not None and outcome.runs
                else _answered_from(final)
            ),
            "source_route": route.decision.as_dict() if route is not None else None,
            "agent_trace": outcome.trace if outcome is not None else None,
            # The database that answered (the supervisor may have switched).
            "connection_id": str(final.get("connection_id") or initial.get("connection_id") or ""),
        }
        if explicit_scope:
            result["source_scope"] = scope.model_dump(mode="json")
        _persist_analysis_state(
            conversation_id=initial.get("conversation_id"),
            question=question,
            final=result,
            tenant_id=initial.get("tenant_id"),
            user_id=initial.get("user_id"),
            connection_id=result["connection_id"] or initial.get("connection_id"),
        )
        return result
