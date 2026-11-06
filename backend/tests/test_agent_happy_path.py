"""T-53: Agent happy-path test with mocked LLM and database execution."""

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


def test_agent_happy_path_question_to_sql_to_rows(monkeypatch) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    connection_id = uuid4()

    schema = {
        "connection_id": str(connection_id),
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [
                    {
                        "name": "quantity",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": False,
                    },
                    {
                        "name": "product_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [
                    {
                        "column": "product_id",
                        "referred_table": "products",
                        "referred_column": "product_id",
                    }
                ],
            },
            {
                "schema_name": "public",
                "table_name": "products",
                "columns": [
                    {
                        "name": "product_id",
                        "data_type": "integer",
                        "nullable": False,
                        "is_pk": True,
                    },
                    {
                        "name": "price",
                        "data_type": "numeric",
                        "nullable": False,
                        "is_pk": False,
                    },
                ],
                "foreign_keys": [],
            },
        ],
    }

    semantic_context = {
        "metrics": [
            {
                "name": "Revenue",
                "expression": "SUM(orders.quantity * products.price)",
            }
        ],
        "dimensions": [],
    }

    class FakeSchemaCache:
        def get(self, tenant_id_value, connection_id_value):
            assert str(tenant_id_value) == str(tenant_id)
            assert str(connection_id_value) == str(connection_id)
            return schema

    class FakeSemanticCache:
        def get(self, tenant_id_value, connection_id_value=None):
            assert str(tenant_id_value) == str(tenant_id)
            if connection_id_value is not None:
                assert str(connection_id_value) == str(connection_id)
            return semantic_context

    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)

            if "intent classifier" in prompt.lower():
                return "sql"

            if "sql generation assistant" in prompt.lower():
                return (
                    "SELECT SUM(o.quantity * p.price) AS revenue "
                    "FROM public.orders o "
                    "JOIN public.products p ON o.product_id = p.product_id"
                )

            if "the previous sql failed" in prompt.lower():
                return (
                    "SELECT SUM(o.quantity * p.price) AS revenue "
                    "FROM public.orders o "
                    "JOIN public.products p ON o.product_id = p.product_id"
                )

            if "pre-computed facts json" in prompt.lower():
                return "Total revenue is 123."

            raise AssertionError(f"Unexpected LLM prompt: {prompt}")

    fake_llm = FakeLLM()
    query_calls: list[tuple[str, str]] = []

    def fake_run_readonly_query(
        connection_id_value: str,
        sql: str,
        *,
        tenant_id=None,  # noqa: ANN001
        **kwargs,  # noqa: ANN002
    ) -> list[dict]:
        query_calls.append((connection_id_value, sql))
        return [{"revenue": 123}]

    monkeypatch.setattr(
        workflow_mod,
        "SchemaCache",
        FakeSchemaCache,
    )
    monkeypatch.setattr(
        workflow_mod,
        "SemanticCache",
        FakeSemanticCache,
    )
    monkeypatch.setattr(
        workflow_mod,
        "get_llm",
        lambda: fake_llm,
    )
    monkeypatch.setattr(
        workflow_mod,
        "run_readonly_query",
        fake_run_readonly_query,
    )

    result = run_question(
        AgentQuestion(
            tenant_id=tenant_id,
            user_id=user_id,
            connection_id=connection_id,
            question="What is the total revenue?",
        )
    )

    assert result.error is None

    assert result.steps == [
        "classify_intent",
        "generate_sql",
        "execute_query",
        "recommend_chart",
        "generate_insight",
    ]

    assert result.sql is not None
    assert "SUM(" in result.sql
    assert "LIMIT 10000" in result.sql

    assert result.rows == [
        {
            "revenue": 123,
        }
    ]

    assert "123" in result.answer
    assert result.chat_grounding is not None

    assert len(query_calls) == 1
    called_connection_id, called_sql = query_calls[0]

    assert called_connection_id == str(connection_id)
    assert "SUM(" in called_sql
    assert "LIMIT 10000" in called_sql

    assert len(fake_llm.prompts) >= 2