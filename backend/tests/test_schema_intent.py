"""Schema intent + missing-schema chat answers."""

from __future__ import annotations

import os
from uuid import uuid4

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from app.config import get_settings

get_settings.cache_clear()

import app.agent.workflow as workflow_mod
from app.agent.service import run_question
from app.agent.types import AgentQuestion
from app.agent.workflow import (
    IntentClassifier,
    SchemaDescriber,
    _clean_sql,
    _parse_intent_label,
)


def test_parse_intent_label_exact_token() -> None:
    assert _parse_intent_label("sql") == "sql"
    assert _parse_intent_label("Schema\nextra") == "schema"
    assert _parse_intent_label("[llm stub] missing") is None


def test_clean_sql_strips_fences_and_stub() -> None:
    assert _clean_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert _clean_sql("[llm stub] missing LLM_API_KEY") is None


def test_greeting_only_not_substring_hi(monkeypatch) -> None:
    class FakeLLM:
        def complete(self, prompt: str) -> str:
            return "[llm stub]"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    result = IntentClassifier().run({"question": "Which customers placed orders?"})
    assert result["intent"] == "sql"


def test_schema_describer_formats_tables() -> None:
    class FakeCache:
        def get(self, tenant_id, connection_id):
            return {
                "db_type": "postgres",
                "tables": [
                    {
                        "table_name": "orders",
                        "columns": [
                            {
                                "name": "order_id",
                                "data_type": "integer",
                                "nullable": False,
                                "is_pk": True,
                            }
                        ],
                        "foreign_keys": [],
                    }
                ],
            }

        def set(self, *args, **kwargs):
            return True

    node = SchemaDescriber(schema_cache=FakeCache())  # type: ignore[arg-type]
    out = node.run(
        {
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "question": "show schema",
        }
    )
    assert out["rows"]
    assert "orders" in out["answer"]
    assert out["rows"][0]["table"] == "orders"


def test_overview_detection() -> None:
    from app.agent.workflow import _requested_question_count, _wants_schema_overview

    assert _wants_schema_overview(
        "tell me what my database is about and what details or insight i can get from it"
    )
    assert _wants_schema_overview("tell me 10 questions i can ask from my db")
    assert _wants_schema_overview("what can I ask?")
    assert not _wants_schema_overview("show schema")
    assert not _wants_schema_overview("what columns does orders have")
    assert not _wants_schema_overview("how many tables are there")
    assert not _wants_schema_overview("list the tables")
    assert _requested_question_count("tell me 10 questions") == 10
    assert _requested_question_count("give me some questions") == 10
    assert _requested_question_count("give me 50 questions") == 20


_OVERVIEW_SCHEMA = {
    "db_type": "postgres",
    "tables": [
        {
            "table_name": "fact_sales",
            "description": "Sales invoice lines.",
            "columns": [
                {"name": "net_sales_value", "data_type": "real", "nullable": True, "is_pk": False}
            ],
            "foreign_keys": [],
        }
    ],
}


class _OverviewCache:
    def get(self, tenant_id, connection_id):
        return _OVERVIEW_SCHEMA

    def set(self, *args, **kwargs):
        return True


def test_schema_describer_overview_uses_llm(monkeypatch) -> None:
    prompts: list[str] = []

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            prompts.append(prompt)
            return "1. What were net sales last month?"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    out = SchemaDescriber(schema_cache=_OverviewCache()).run(  # type: ignore[arg-type]
        {
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "question": "tell me 7 questions i can ask from my db",
        }
    )
    assert out["answer"] == "1. What were net sales last month?"
    assert out["rows"] == []
    assert "exactly 7" in prompts[0]
    assert "fact_sales" in prompts[0]


def test_schema_describer_overview_fallback_has_no_column_dump(monkeypatch) -> None:
    class StubLLM:
        def complete(self, prompt: str) -> str:
            return "[llm stub] missing LLM_API_KEY"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: StubLLM())
    out = SchemaDescriber(schema_cache=_OverviewCache()).run(  # type: ignore[arg-type]
        {
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "question": "what is my database about",
        }
    )
    assert "fact_sales — Sales invoice lines." in out["answer"]
    assert "net_sales_value" not in out["answer"]


def test_run_question_schema_path(monkeypatch) -> None:
    schema = {
        "db_type": "postgres",
        "tables": [
            {
                "table_name": "orders",
                "columns": [
                    {
                        "name": "amount",
                        "data_type": "numeric",
                        "nullable": True,
                        "is_pk": False,
                    }
                ],
                "foreign_keys": [],
            }
        ],
    }

    class FakeLLM:
        def complete(self, prompt: str) -> str:
            if "Classify the user's question" in prompt or "intent classifier" in prompt.lower():
                return "schema"
            return "unused"

    class FakeCache:
        def get(self, tenant_id, connection_id):
            return schema

        def set(self, *args, **kwargs):
            return True

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        workflow_mod,
        "SchemaCache",
        lambda: FakeCache(),
    )
    # SchemaDescriber / SQLGenerator construct SchemaCache() internally via default;
    # also patch _resolve_schema to return our schema.
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda tenant_id, connection_id, cache: schema,
    )

    result = run_question(
        AgentQuestion(
            tenant_id=uuid4(),
            user_id=uuid4(),
            question="tell me the schema",
            connection_id=uuid4(),
        )
    )
    assert result.intent == "schema"
    assert "describe_schema" in result.steps
    assert "generate_sql" not in result.steps
    assert "orders" in result.answer
    assert result.rows


def test_run_question_missing_schema_gives_clear_answer(monkeypatch) -> None:
    class FakeLLM:
        def complete(self, prompt: str) -> str:
            if "Classify" in prompt or "intent classifier" in prompt.lower():
                return "sql"
            return "should not be used for insight when error set"

    monkeypatch.setattr(workflow_mod, "get_llm", lambda: FakeLLM())
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda tenant_id, connection_id, cache: None,
    )

    result = run_question(
        AgentQuestion(
            tenant_id=uuid4(),
            user_id=uuid4(),
            question="show me the order table insight",
            connection_id=uuid4(),
        )
    )
    assert result.intent == "sql"
    assert result.sql is None
    assert result.error
    assert "Discover schema" in result.answer or "schema" in result.answer.lower()
