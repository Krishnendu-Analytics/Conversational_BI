"""Check that run_question walks the LangGraph stub paths."""

from __future__ import annotations

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from app.config import get_settings

get_settings.cache_clear()

from app.agent.service import run_question
from app.agent.types import AgentQuestion


def test_run_question_sql_stub_path() -> None:
    result = run_question(
        AgentQuestion(
            tenant_id=uuid4(),
            user_id=uuid4(),
            question="show revenue",
            connection_id=uuid4(),
        )
    )
    # Without discover, SQL is skipped and the answer tells the user what to do.
    assert result.error
    assert "schema" in result.error.lower()
    assert result.steps == [
        "classify_intent",
        "generate_sql",
        "execute_query",
        "recommend_chart",
        "generate_insight",
    ]
    assert result.answer
    assert "Discover schema" in result.answer or "schema" in result.answer.lower()


def test_run_question_greeting_skips_sql() -> None:
    result = run_question(
        AgentQuestion(
            tenant_id=uuid4(),
            user_id=uuid4(),
            question="hello",
            connection_id=uuid4(),
        )
    )
    assert result.error is None
    assert result.steps == [
        "classify_intent",
        "generate_insight",
    ]
    assert "generate_sql" not in result.steps
    assert "execute_query" not in result.steps
    assert "Hello" in result.answer
