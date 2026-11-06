from __future__ import annotations

from uuid import uuid4

import app.agent.workflow as workflow_mod
from app.agent.workflow import InsightGenerator, QueryExecutor


def test_query_executor_validates_sql_and_executes(monkeypatch) -> None:
    schema = {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "table_name": "orders",
                "columns": [
                    {"name": "amount"},
                ],
                "foreign_keys": [],
            }
        ],
    }

    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda _tenant_id, _connection_id, _cache: schema,
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda _tenant_id, _connection_id, _cache: {},
    )

    class FakeSafetyPipeline:
        def __init__(self, schema=None, db_type=None, **kwargs):  # noqa: ANN001
            self.schema = schema
            self.db_type = db_type
            self.validated = []

        def validate(self, sql: str) -> str:
            self.validated.append(sql)
            return "SELECT amount FROM orders LIMIT 1"

    monkeypatch.setattr(workflow_mod, "SQLSafetyPipeline", FakeSafetyPipeline)

    calls: list[tuple[str, str]] = []

    def fake_run_readonly_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        calls.append((connection_id, sql))
        return [{"amount": 123}]

    monkeypatch.setattr(
        workflow_mod,
        "run_readonly_query",
        fake_run_readonly_query,
    )

    tenant_id = uuid4()
    qe = QueryExecutor()
    result = qe.run(
        {
            "tenant_id": str(tenant_id),
            "connection_id": "conn-1",
            "sql": "SELECT amount FROM orders",
        }
    )

    assert result["rows"] == [{"amount": 123}]
    assert result["sql"] == "SELECT amount FROM orders LIMIT 1"
    assert calls == [("conn-1", "SELECT amount FROM orders LIMIT 1")]
    assert "execute_query" in result["steps"]


def test_query_executor_recovers_with_fallback_on_db_column_error(
    monkeypatch,
) -> None:
    schema = {
        "connection_id": "connection-123",
        "db_type": "postgres",
        "tables": [
            {
                "table_name": "orders",
                "columns": [
                    {"name": "amount", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            },
            {
                "table_name": "agreements",
                "columns": [
                    {"name": "amount_financed", "data_type": "numeric"},
                ],
                "foreign_keys": [],
            },
        ],
    }

    monkeypatch.setattr(
        workflow_mod,
        "_resolve_schema",
        lambda _tenant_id, _connection_id, _cache: schema,
    )
    monkeypatch.setattr(
        workflow_mod,
        "_resolve_semantic",
        lambda _tenant_id, _connection_id, _cache: {},
    )

    class PassthroughSafety:
        def __init__(self, schema=None, db_type=None, **kwargs):  # noqa: ANN001
            pass

        def validate(self, sql: str) -> str:
            return sql

    monkeypatch.setattr(workflow_mod, "SQLSafetyPipeline", PassthroughSafety)

    calls: list[str] = []

    def fail_then_succeed(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        calls.append(sql)
        if "agreement_number" in sql.lower():
            raise RuntimeError(
                "UndefinedColumn: column agreement_number does not exist"
            )
        return [{"row_count": 5}]

    monkeypatch.setattr(workflow_mod, "run_readonly_query", fail_then_succeed)
    monkeypatch.setattr(
        workflow_mod,
        "get_llm",
        lambda: type(
            "LLM",
            (),
            {
                "complete": lambda self, p: (
                    "SELECT COUNT(agreement_number) FROM agreements"
                )
            },
        )(),
    )

    tenant_id = uuid4()
    qe = QueryExecutor()
    result = qe.run(
        {
            "tenant_id": str(tenant_id),
            "connection_id": "conn-1",
            "question": "dashboard overview",
            "sql": "SELECT COUNT(agreement_number) FROM agreements",
        }
    )

    assert result.get("error") is None
    assert result["rows"] == [{"row_count": 5}]
    assert len(calls) == 2
    assert "agreement_number" in calls[0].lower()
    assert "agreement_number" not in calls[1].lower()


def test_insight_generator_calls_llm(monkeypatch) -> None:
    class FakeLLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "FINAL ANSWER"

    fake_llm = FakeLLM()
    monkeypatch.setattr(workflow_mod, "get_llm", lambda: fake_llm)

    ig = InsightGenerator()
    result = ig.run(
        {
            "intent": "sql",
            "question": "What is the revenue?",
            "sql": "SELECT amount FROM orders LIMIT 1",
            "rows": [{"amount": 100}],
        }
    )

    assert result["answer"] == "FINAL ANSWER"
    assert result["steps"] == ["generate_insight"]
    assert result.get("chat_grounding")
    assert len(fake_llm.prompts) == 1
    assert "What is the revenue?" in fake_llm.prompts[0]
    assert "facts" in fake_llm.prompts[0].lower() or "kpis" in fake_llm.prompts[0]
