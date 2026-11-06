"""Simple types passed between the API and the agent."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class AgentQuestion:
    tenant_id: UUID
    user_id: UUID
    question: str
    connection_id: UUID
    conversation_id: UUID | None = None
    document_ids: list[UUID] = field(default_factory=list)
    # {"source": "auto"|"documents"|"database", "document_ids": [...]}; None
    # keeps the conversation's current scope.
    source_scope: dict[str, Any] | None = None


@dataclass
class AgentResult:
    answer: str
    sql: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    rag_chunks: list[dict[str, Any]] = field(default_factory=list)
    rag_context: str | None = None
    chart_suggestion: str | None = None
    dashboard: dict[str, Any] | None = None
    steps: list[str] = field(default_factory=list)
    error: str | None = None
    intent: str | None = None
    run_id: str | None = None
    chat_grounding: dict[str, Any] | None = None
    query_clarification: dict[str, Any] | None = None
    dataset_id: str | None = None
    dataset_display_name: str | None = None
    dataset_clarification: dict[str, Any] | None = None
    answered_from: dict[str, Any] | None = None
    # The connection that answered (the supervisor may pick another database).
    connection_id: str | None = None
    # Files that joined the conversation this turn (attached, @-picked, or
    # chosen by the auto source router).
    documents_added: list[dict[str, Any]] = field(default_factory=list)
    source_scope: dict[str, Any] | None = None
