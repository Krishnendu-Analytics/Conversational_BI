"""T-58: Redis progress publisher + LangGraph emit hooks."""

from __future__ import annotations

import json
from uuid import uuid4

import app.agent.workflow as workflow_mod
from app.agent.events import (
    IntentDetectedEvent,
    ProgressEventType,
    parse_progress_event,
)
from app.agent.service import run_question
from app.agent.types import AgentQuestion
from app.redis_client import RedisPublisher, get_redis


class _MemoryRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, _ttl: int, value: str) -> bool:
        self.store[key] = value
        return True

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 0

    def ping(self) -> bool:
        return True


def test_redis_publisher_publishes_typed_event(monkeypatch) -> None:
    memory = _MemoryRedis()
    client = get_redis()
    client.reset()
    monkeypatch.setattr(client, "_connect", lambda: memory)
    monkeypatch.setattr(client, "_failed", False)

    event = IntentDetectedEvent(
        run_id="run-1",
        tenant_id="tenant-1",
        user_id="user-1",
        conversation_id="conv-1",
        intent="sql",
        question="show revenue",
    )
    assert RedisPublisher().publish_event(event) is True
    assert len(memory.published) == 1
    channel, body = memory.published[0]
    assert channel == "progress:tenant-1:conv-1"
    parsed = parse_progress_event(json.loads(body))
    assert parsed.type == ProgressEventType.INTENT_DETECTED


def test_workflow_emits_progress_events(monkeypatch) -> None:
    memory = _MemoryRedis()
    client = get_redis()
    client.reset()
    monkeypatch.setattr(client, "_connect", lambda: memory)
    monkeypatch.setattr(client, "_failed", False)

    # Force deterministic SQL intent via classifier fallback (LLM stub).
    tenant_id = uuid4()
    user_id = uuid4()
    conversation_id = uuid4()
    result = run_question(
        AgentQuestion(
            tenant_id=tenant_id,
            user_id=user_id,
            question="show revenue",
            connection_id=uuid4(),
            conversation_id=conversation_id,
        )
    )
    # Missing schema is an expected, clear failure — still emits progress.
    assert result.error
    assert result.run_id

    types = [json.loads(body)["type"] for _, body in memory.published]
    assert ProgressEventType.INTENT_DETECTED.value in types
    assert ProgressEventType.SQL_GENERATED.value in types
    assert ProgressEventType.ERROR.value in types
    assert ProgressEventType.QUERY_EXECUTED.value in types
    assert ProgressEventType.INSIGHT_READY.value in types

    channel = f"progress:{tenant_id}:{conversation_id}"
    assert all(ch == channel for ch, _ in memory.published)


def test_rag_path_steps_and_state_merge(monkeypatch) -> None:
    """Force RAG intent and confirm retrieve_docs merges rag_* fields."""

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            if "intent classifier" in prompt.lower() or "Classify the user's question" in prompt:
                return "rag"
            return "unused"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())

    result = run_question(
        AgentQuestion(
            tenant_id=uuid4(),
            user_id=uuid4(),
            question="What is our refund policy?",
            connection_id=uuid4(),
        )
    )
    assert result.steps == [
        "classify_intent",
        "retrieve_docs",
        "generate_insight",
    ]
    assert result.intent == "rag"
    assert result.rag_context == "" or result.rag_context is None or isinstance(result.rag_context, str)
    assert isinstance(result.rag_chunks, list)
    # Empty corpus → insufficient / no hallucinated stub corpus
    assert result.rag_chunks == []
    assert "document" in (result.answer or "").lower() or "database" in (result.answer or "").lower()


def test_hybrid_path_runs_sql_then_rag(monkeypatch) -> None:
    class FakeLLM:
        def complete(self, prompt: str) -> str:
            if "Classify the user's question" in prompt:
                return "hybrid"
            if "Generate a SQL query" in prompt or "SQL generation" in prompt:
                return "SELECT 1"
            return "hybrid answer"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())

    result = run_question(
        AgentQuestion(
            tenant_id=uuid4(),
            user_id=uuid4(),
            question="revenue and the policy doc",
            connection_id=uuid4(),
        )
    )
    assert result.intent == "hybrid"
    assert result.steps == [
        "classify_intent",
        "generate_sql",
        "execute_query",
        "retrieve_docs",
        "recommend_chart",
        "generate_insight",
    ]
    assert result.rag_context is not None
