"""P0-F: Read-only execution audit — all app SQL paths enforce SELECT-only."""

from __future__ import annotations

import inspect
import re

import pytest

from app.agent.dashboard import _validate_and_run
from app.schema_profiling import profile_table
from app.tools.customer_db import run_readonly_query
from app.tools.sql_safety import SQLSafetyPipeline


def test_run_readonly_query_expects_pipeline_validation() -> None:
    doc = inspect.getdoc(run_readonly_query) or ""
    assert "SQLSafetyPipeline" in doc


def test_validate_and_run_uses_pipeline_before_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    called = {"pipeline": False, "query": False}

    class SpyPipeline:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def validate(self, sql: str) -> str:
            called["pipeline"] = True
            assert sql.strip().upper().startswith("SELECT")
            return sql

    def spy_query(connection_id: str, sql: str, *, tenant_id=None, **kwargs):  # noqa: ANN001
        called["query"] = True
        return []

    monkeypatch.setattr("app.agent.dashboard.SQLSafetyPipeline", SpyPipeline)
    monkeypatch.setattr("app.agent.dashboard.run_readonly_query", spy_query)

    _validate_and_run(
        str(uuid4()),
        {"db_type": "postgres", "tables": []},
        "postgres",
        "SELECT 1 AS value",
        tenant_id=uuid4(),
    )
    assert called["pipeline"] is True
    assert called["query"] is True


def test_profile_table_generates_select_only_sql() -> None:
    class SpyProvider:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def execute_read_only_query(self, sql: str, **kwargs) -> list[dict]:
            self.queries.append(sql.strip())
            if "COUNT(*)" in sql.upper():
                return [{"cnt": 0}]
            return []

    provider = SpyProvider()
    profile_table(
        provider,
        db_type="postgres",
        schema_name="public",
        table_name="orders",
        columns=[
            {"name": "amount", "data_type": "numeric"},
            {"name": "region", "data_type": "varchar"},
        ],
    )
    assert provider.queries
    for sql in provider.queries:
        assert re.match(r"^\s*SELECT\b", sql, re.IGNORECASE), sql


def test_sql_safety_pipeline_rejects_write_sql() -> None:
    pipeline = SQLSafetyPipeline(schema={"db_type": "postgres", "tables": []}, db_type="postgres")
    with pytest.raises(Exception):
        pipeline.validate("DELETE FROM public.orders")
