"""
Clients subscribe to Redis pub/sub (via the chat WebSocket bridge) and receive
these typed events as the LangGraph workflow advances.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProgressEventType(str, Enum):
    """Canonical progress event names pushed to the client."""

    INTENT_DETECTED = "intent_detected"
    SQL_GENERATED = "sql_generated"
    QUERY_EXECUTED = "query_executed"
    INSIGHT_READY = "insight_ready"
    DASHBOARD_UPDATED = "dashboard_update"
    ERROR = "error"
    INVESTIGATION_STEP = "investigation_step"
    INVESTIGATION_COMPLETE = "investigation_complete"


class ProgressEventBase(BaseModel):
    """Fields shared by every progress event."""

    type: ProgressEventType
    run_id: str
    tenant_id: str
    user_id: str
    conversation_id: str | None = None
    connection_id: str | None = None
    step: str
    message: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class IntentDetectedEvent(ProgressEventBase):
    type: Literal[ProgressEventType.INTENT_DETECTED] = ProgressEventType.INTENT_DETECTED
    step: str = "classify_intent"
    intent: str
    question: str = ""
    # The LLM's own classification before any heuristic override, so
    # misrouting (heuristic disagreeing with the model) is visible in
    # telemetry instead of only the final intent.
    llm_intent: str | None = None


class SqlGeneratedEvent(ProgressEventBase):
    type: Literal[ProgressEventType.SQL_GENERATED] = ProgressEventType.SQL_GENERATED
    step: str = "generate_sql"
    sql: str | None = None


class QueryExecutedEvent(ProgressEventBase):
    type: Literal[ProgressEventType.QUERY_EXECUTED] = ProgressEventType.QUERY_EXECUTED
    step: str = "execute_query"
    sql: str | None = None
    row_count: int = 0
    # Cap preview size so pub/sub payloads stay small.
    rows_preview: list[dict[str, Any]] = Field(default_factory=list)


class InsightReadyEvent(ProgressEventBase):
    type: Literal[ProgressEventType.INSIGHT_READY] = ProgressEventType.INSIGHT_READY
    step: str = "generate_insight"
    answer: str
    sql: str | None = None
    chart_suggestion: str | None = None
    intent: str | None = None


class DashboardUpdatedEvent(ProgressEventBase):
    type: Literal[ProgressEventType.DASHBOARD_UPDATED] = (
        ProgressEventType.DASHBOARD_UPDATED
    )
    step: str = "edit_dashboard"
    dashboard_id: str
    dashboard: dict[str, Any]
    edit_notes: list[str] = Field(default_factory=list)


class ErrorEvent(ProgressEventBase):
    type: Literal[ProgressEventType.ERROR] = ProgressEventType.ERROR
    step: str = "error"
    error: str
    detail: str | None = None


class InvestigationStepEvent(ProgressEventBase):
    """One domain checked (or skipped) during a multi-domain investigation.

    Fired once per domain as the investigator loop (app.agent.investigator)
    runs, so the client can show live "Checking Sales... Checking
    Production..." progress instead of a single opaque wait — the same
    live-streaming UX the existing events already give single-query chat.
    """

    type: Literal[ProgressEventType.INVESTIGATION_STEP] = (
        ProgressEventType.INVESTIGATION_STEP
    )
    step: str = "investigate"
    domain: str = ""


class InvestigationCompleteEvent(ProgressEventBase):
    type: Literal[ProgressEventType.INVESTIGATION_COMPLETE] = (
        ProgressEventType.INVESTIGATION_COMPLETE
    )
    step: str = "investigate"
    domains_checked: int = 0


ProgressEvent = (
    IntentDetectedEvent
    | SqlGeneratedEvent
    | QueryExecutedEvent
    | InsightReadyEvent
    | DashboardUpdatedEvent
    | ErrorEvent
    | InvestigationStepEvent
    | InvestigationCompleteEvent
)


def parse_progress_event(payload: dict[str, Any]) -> ProgressEvent:
    """Validate a raw JSON dict into a typed progress event."""
    event_type = payload.get("type")
    mapping: dict[str, type[ProgressEventBase]] = {
        ProgressEventType.INTENT_DETECTED.value: IntentDetectedEvent,
        ProgressEventType.SQL_GENERATED.value: SqlGeneratedEvent,
        ProgressEventType.QUERY_EXECUTED.value: QueryExecutedEvent,
        ProgressEventType.INSIGHT_READY.value: InsightReadyEvent,
        ProgressEventType.DASHBOARD_UPDATED.value: DashboardUpdatedEvent,
        ProgressEventType.ERROR.value: ErrorEvent,
        ProgressEventType.INVESTIGATION_STEP.value: InvestigationStepEvent,
        ProgressEventType.INVESTIGATION_COMPLETE.value: InvestigationCompleteEvent,
    }
    model = mapping.get(str(event_type))
    if model is None:
        raise ValueError(f"Unknown progress event type: {event_type}")
    return model.model_validate(payload)  # type: ignore[return-value]


def progress_channel(
    tenant_id: str,
    *,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> str:
    """Redis pub/sub channel for a live chat session.

    Prefer conversation scope when available; fall back to user scope.
    """
    scope = conversation_id or user_id or "anonymous"
    return f"progress:{tenant_id}:{scope}"
