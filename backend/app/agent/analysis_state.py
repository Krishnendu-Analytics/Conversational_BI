"""Structured persistent conversation analysis state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.agent.query_plan import FilterSpec, QueryPlan, TimeSpec


MAX_SQL_LEN = 8192
MAX_SAMPLE_ROWS = 5
MAX_SAMPLE_COLS = 8
MAX_CELL_LEN = 120


class ResultSummary(BaseModel):
    row_count: int = 0
    columns: list[str] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list)
    headline: str | None = None


class SemanticFocus(BaseModel):
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)


MAX_INVESTIGATION_EVIDENCE = 10


class InvestigationFocus(BaseModel):
    """Durable trace of the most recent multi-domain investigation for this
    conversation. Investigator's own ``InvestigationState.evidence`` is
    ephemeral (lives only for one graph run) — this is what lets a
    follow-up turn, or a later look at the conversation, see what was
    checked and found without re-running the investigation."""

    run_id: str | None = None
    question: str | None = None
    domains_checked: list[str] = Field(default_factory=list)
    evidence_summary: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime | None = None


class DashboardFocus(BaseModel):
    dashboard_id: UUID | None = None
    active_widget_id: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    title: str | None = None
    widget_catalog: list[dict[str, Any]] = Field(default_factory=list)
    pending_operation: dict[str, Any] | None = None
    last_edit_notes: list[str] = Field(default_factory=list)


MAX_RECENT_TURNS = 5
MAX_TURN_QUESTION_LEN = 300
MAX_TURN_ANSWER_LEN = 600


MAX_CONVERSATION_DOCUMENTS = 20


class ConversationDocument(BaseModel):
    """A file attached somewhere in this conversation; later turns can use it."""

    id: UUID
    filename: str = ""


class ConversationTurn(BaseModel):
    """One question/answer exchange, trimmed — enough for the model to follow
    the thread ("what about last quarter?", "why is that?") without the
    whole transcript."""

    question: str
    answer: str = ""


SourceKind = Literal["auto", "documents", "database"]


class SourceScope(BaseModel):
    """What the user pointed the chat at (the ``@`` picker). Sticky: it stays
    until the user changes it, so a follow-up about an attached PDF is still
    answered from that PDF instead of drifting back to the database.

    ``auto``: the router decides (database, plus any files in the chat).
    ``documents``: answer only from files — ``document_ids``, or all of this
    chat's files when empty. ``database``: answer only from the connection."""

    source: SourceKind = "auto"
    document_ids: list[UUID] = Field(default_factory=list)


class RecalledChat(BaseModel):
    """A related earlier conversation surfaced by long-term memory."""

    title: str = ""
    summary: str


class AnalysisState(BaseModel):
    version: int = 1
    connection_id: UUID | None = None
    dataset_id: UUID | None = None
    dataset_display_name: str | None = None
    last_intent: str | None = None
    query_plan: QueryPlan | None = None
    last_sql: str | None = None
    result_summary: ResultSummary | None = None
    semantic_focus: SemanticFocus = Field(default_factory=SemanticFocus)
    dashboard: DashboardFocus = Field(default_factory=DashboardFocus)
    investigation: InvestigationFocus = Field(default_factory=InvestigationFocus)
    last_user_question: str | None = None
    last_assistant_answer: str | None = None
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    turn_count: int = 0
    updated_at: datetime | None = None
    pending_query_clarification: dict[str, Any] | None = None
    pending_dataset_clarification: dict[str, Any] | None = None
    # Files attached in this conversation, oldest first (like a ChatGPT chat).
    documents: list[ConversationDocument] = Field(default_factory=list)
    source_scope: SourceScope = Field(default_factory=SourceScope)
    # Memory, filled per turn by app.services.memory_service (not persisted
    # here: it lives in user_memories and is recalled fresh each turn).
    conversation_summary: str | None = None
    user_facts: list[str] = Field(default_factory=list)
    related_chats: list[RecalledChat] = Field(default_factory=list)


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _truncate_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_CELL_LEN:
        return value[: MAX_CELL_LEN - 1] + "…"
    return value


def summarize_rows(
    rows: list[dict[str, Any]] | None,
    *,
    headline: str | None = None,
) -> ResultSummary | None:
    if not rows:
        return ResultSummary(row_count=0, headline=headline)

    columns: list[str] = []
    if isinstance(rows[0], dict):
        columns = list(rows[0].keys())[:MAX_SAMPLE_COLS]

    sample: list[dict[str, Any]] = []
    for row in rows[:MAX_SAMPLE_ROWS]:
        if not isinstance(row, dict):
            continue
        trimmed = {
            key: _truncate_value(row.get(key))
            for key in list(row.keys())[:MAX_SAMPLE_COLS]
        }
        sample.append(trimmed)

    return ResultSummary(
        row_count=len(rows),
        columns=columns,
        sample_rows=sample,
        headline=headline,
    )


def analysis_state_from_json(raw: str | None) -> AnalysisState:
    if not raw or not raw.strip():
        return AnalysisState()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return AnalysisState()
    if not isinstance(data, dict):
        return AnalysisState()
    try:
        return AnalysisState.model_validate(data)
    except Exception:  # noqa: BLE001
        return AnalysisState()


def analysis_state_to_json(state: AnalysisState) -> str:
    payload = state.model_dump(mode="json")
    return json.dumps(payload, default=str)


def _investigation_evidence_summary(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Trim raw DomainEvidence dicts to what's worth remembering across
    turns: which domains resolved, which didn't, and why — not full SQL or
    row-level facts (those already live, per-run, in the audit trail)."""
    summary: list[dict[str, Any]] = []
    for raw in evidence[:MAX_INVESTIGATION_EVIDENCE]:
        if not isinstance(raw, dict):
            continue
        summary.append(
            {
                "domain": raw.get("domain"),
                "ok": raw.get("error") is None and raw.get("facts") is not None,
                "error": raw.get("error"),
            }
        )
    return summary


def sync_semantic_focus_from_plan(state: AnalysisState, plan: QueryPlan | None) -> None:
    if plan is None:
        return
    state.semantic_focus.metrics = list(plan.metrics or [])
    state.semantic_focus.dimensions = list(plan.dimensions or [])


def format_analysis_state_context(state: AnalysisState | None) -> str:
    """Structured analyst context for LLM prompts (not full transcript)."""
    if state is None:
        return "None."
    lines: list[str] = _memory_context_lines(state)
    if state.turn_count == 0 and not state.last_sql and not state.query_plan:
        return "\n".join(lines) if lines else "None."

    if state.connection_id:
        lines.append(f"Active connection: {state.connection_id}")
    if state.dataset_id:
        label = state.dataset_display_name or str(state.dataset_id)
        lines.append(f"Active dataset: {label} (id={state.dataset_id})")
    if state.last_intent:
        lines.append(f"Last intent: {state.last_intent}")
    if state.semantic_focus.metrics:
        lines.append(f"Metrics: {', '.join(state.semantic_focus.metrics)}")
    if state.semantic_focus.dimensions:
        lines.append(f"Dimensions: {', '.join(state.semantic_focus.dimensions)}")
    if state.semantic_focus.tables:
        lines.append(f"Tables: {', '.join(state.semantic_focus.tables)}")

    plan = state.query_plan
    if plan and plan.filters:
        filter_bits = []
        for filt in plan.filters:
            filter_bits.append(f"{filt.field} {filt.operator} {filt.value!r}")
        lines.append(f"Filters: {', '.join(filter_bits)}")
    if plan and plan.time and plan.time.range:
        lines.append(f"Time range: {plan.time.range}")

    if state.last_sql:
        sql_preview = state.last_sql[:500]
        if len(state.last_sql) > 500:
            sql_preview += "…"
        lines.append(f"Previous SQL: {sql_preview}")

    summary = state.result_summary
    if summary:
        if summary.headline:
            lines.append(f"Result summary: {summary.headline}")
        elif summary.row_count:
            cols = ", ".join(summary.columns[:6]) if summary.columns else "n/a"
            lines.append(f"Result summary: {summary.row_count} rows ({cols})")

    dash = state.dashboard
    if dash.dashboard_id or dash.title:
        widget = f", widget={dash.active_widget_id}" if dash.active_widget_id else ""
        lines.append(
            f"Active dashboard: {dash.title or 'Dashboard'} "
            f"(id={dash.dashboard_id}{widget})"
        )
        if dash.filters:
            lines.append(f"Dashboard filters: {json.dumps(dash.filters, default=str)[:200]}")

    if state.conversation_summary and state.turn_count > len(state.recent_turns):
        # Turns older than the recent window are only kept as this summary.
        lines.append(f"Earlier in this conversation (summary): {state.conversation_summary}")
    if state.recent_turns:
        # The actual thread, not just the last question: lets the model
        # resolve "that", "same for dispatch", "why?" against what was said.
        lines.append("Recent conversation (oldest first):")
        for turn in state.recent_turns:
            lines.append(f"  User: {turn.question}")
            if turn.answer:
                lines.append(f"  Assistant: {' '.join(turn.answer.split())}")
    elif state.last_user_question:
        lines.append(f"Last user question: {state.last_user_question[:300]}")

    inv = state.investigation
    if inv.domains_checked:
        failed = [e["domain"] for e in inv.evidence_summary if not e.get("ok")]
        line = f"Last investigation ({inv.question or 'unspecified'}): checked {', '.join(inv.domains_checked)}"
        if failed:
            line += f"; no evidence from {', '.join(failed)}"
        lines.append(line)

    return "\n".join(lines) if lines else "None."


def _memory_context_lines(state: AnalysisState) -> list[str]:
    """Long-term memory: what we know about this user, and related earlier
    chats. Background only — the current conversation always wins."""
    lines: list[str] = []
    if state.user_facts:
        lines.append("What you remember about this user (from earlier chats):")
        lines.extend(f"  - {fact}" for fact in state.user_facts)
    if state.related_chats:
        lines.append("Related earlier conversations (background only):")
        for chat in state.related_chats:
            title = f"{chat.title}: " if chat.title else ""
            lines.append(f"  - {title}{chat.summary}")
    return lines


def merge_workflow_into_state(
    prior: AnalysisState,
    *,
    question: str,
    final: dict[str, Any],
    connection_id: UUID | str | None = None,
) -> AnalysisState:
    """Build updated analysis state from a completed workflow turn."""
    state = prior.model_copy(deep=True)
    state.turn_count = int(state.turn_count or 0) + 1
    state.updated_at = datetime.now(timezone.utc)
    state.last_user_question = question.strip() or None
    answer = str(final.get("answer") or "").strip()
    state.last_assistant_answer = answer or None
    state.last_intent = final.get("intent")
    if question.strip():
        state.recent_turns = [
            *state.recent_turns,
            ConversationTurn(
                question=_trim(question.strip(), MAX_TURN_QUESTION_LEN),
                answer=_trim(answer, MAX_TURN_ANSWER_LEN),
            ),
        ][-MAX_RECENT_TURNS:]

    if connection_id is not None:
        try:
            state.connection_id = UUID(str(connection_id))
        except (ValueError, TypeError):
            pass

    # Files attached this turn join the conversation's documents.
    known = {d.id for d in state.documents}
    for raw in final.get("attached_documents") or []:
        try:
            doc = ConversationDocument.model_validate(raw)
        except Exception:  # noqa: BLE001
            continue
        if doc.id not in known:
            state.documents.append(doc)
            known.add(doc.id)
    state.documents = state.documents[-MAX_CONVERSATION_DOCUMENTS:]

    raw_scope = final.get("source_scope")
    if isinstance(raw_scope, dict):
        try:
            state.source_scope = SourceScope.model_validate(raw_scope)
        except Exception:  # noqa: BLE001
            pass

    raw_plan = final.get("query_plan")
    if isinstance(raw_plan, dict):
        try:
            state.query_plan = QueryPlan.model_validate(raw_plan)
            sync_semantic_focus_from_plan(state, state.query_plan)
        except Exception:  # noqa: BLE001
            pass
    elif isinstance(raw_plan, QueryPlan):
        state.query_plan = raw_plan
        sync_semantic_focus_from_plan(state, state.query_plan)

    sql = final.get("sql")
    if isinstance(sql, str) and sql.strip():
        state.last_sql = sql[:MAX_SQL_LEN]
        state.pending_query_clarification = None
        state.pending_dataset_clarification = None
    elif final.get("query_clarification"):
        clarification = final.get("query_clarification")
        state.pending_query_clarification = (
            clarification if isinstance(clarification, dict) else None
        )
    elif final.get("dataset_clarification"):
        clarification = final.get("dataset_clarification")
        state.pending_dataset_clarification = (
            clarification if isinstance(clarification, dict) else None
        )

    raw_dataset_id = final.get("dataset_id")
    if raw_dataset_id:
        try:
            state.dataset_id = UUID(str(raw_dataset_id))
        except (ValueError, TypeError):
            pass
    display = final.get("dataset_display_name")
    if isinstance(display, str) and display.strip():
        state.dataset_display_name = display.strip()

    rows = final.get("rows")
    if isinstance(rows, list):
        headline = answer[:200] if answer else None
        state.result_summary = summarize_rows(rows, headline=headline)

    dashboard = final.get("dashboard")
    if isinstance(dashboard, dict):
        dash_id = dashboard.get("id")
        if dash_id:
            try:
                state.dashboard.dashboard_id = UUID(str(dash_id))
            except (ValueError, TypeError):
                pass
        title = dashboard.get("title") or dashboard.get("name")
        if title:
            state.dashboard.title = str(title)
        filters = dashboard.get("filters")
        if isinstance(filters, list):
            state.dashboard.filters = filters
        widgets = dashboard.get("widgets") or []
        if widgets and isinstance(widgets[0], dict):
            wid = widgets[0].get("id")
            if wid:
                state.dashboard.active_widget_id = str(wid)

    pending = final.get("dashboard_pending_operation")
    if pending is not None:
        state.dashboard.pending_operation = pending if isinstance(pending, dict) else None
    elif final.get("dashboard_edit_applied"):
        state.dashboard.pending_operation = None

    catalog = final.get("dashboard_widget_catalog")
    if isinstance(catalog, list):
        state.dashboard.widget_catalog = catalog

    notes = final.get("dashboard_edit_notes")
    if isinstance(notes, list):
        state.dashboard.last_edit_notes = [str(n) for n in notes]

    active_wid = final.get("dashboard_active_widget_id")
    if active_wid:
        state.dashboard.active_widget_id = str(active_wid)
    else:
        modified = final.get("dashboard_modified_widget_ids")
        if isinstance(modified, list) and modified and final.get("dashboard_edit_applied"):
            state.dashboard.active_widget_id = str(modified[0])

    evidence = final.get("investigation_evidence")
    if isinstance(evidence, list):
        domains = final.get("investigation_domains_checked")
        state.investigation = InvestigationFocus(
            run_id=final.get("run_id"),
            question=question.strip() or None,
            domains_checked=(
                [str(d) for d in domains] if isinstance(domains, list) else []
            ),
            evidence_summary=_investigation_evidence_summary(evidence),
            updated_at=datetime.now(timezone.utc),
        )

    return state


def analysis_state_to_memory_blob(state: AnalysisState) -> dict[str, Any]:
    """Map AnalysisState to legacy ConversationMemory fields for Redis cache."""
    plan = state.query_plan
    filters = [f.model_dump() for f in (plan.filters if plan else [])]
    return {
        "last_sql": state.last_sql,
        "filters": filters,
        "tables": list(state.semantic_focus.tables),
        "metrics": list(state.semantic_focus.metrics),
        "messages": _legacy_messages_from_state(state),
        "dashboard": (
            {
                "id": str(state.dashboard.dashboard_id),
                "title": state.dashboard.title,
                "filters": state.dashboard.filters,
            }
            if state.dashboard.dashboard_id
            else None
        ),
        "analysis_state": state.model_dump(mode="json"),
    }


def _legacy_messages_from_state(state: AnalysisState) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if state.last_user_question:
        messages.append({"role": "user", "content": state.last_user_question})
    if state.last_assistant_answer:
        messages.append({"role": "assistant", "content": state.last_assistant_answer})
    return messages


def memory_blob_to_analysis_state(blob: dict[str, Any] | None) -> AnalysisState | None:
    if not blob:
        return None
    embedded = blob.get("analysis_state")
    if isinstance(embedded, dict):
        try:
            return AnalysisState.model_validate(embedded)
        except Exception:  # noqa: BLE001
            pass
    state = AnalysisState()
    state.last_sql = blob.get("last_sql")
    if blob.get("last_sql"):
        state.turn_count = max(state.turn_count, 1)
    return state
