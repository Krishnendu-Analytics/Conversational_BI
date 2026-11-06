"""P1-2: Query reliability, resource limits, and safe execution."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from app.agent.dashboard import _execute_dashboard_widgets_bounded, execute_dashboard
from app.agent.workflow import QueryExecutor
from app.config import get_settings
from app.query_execution import (
    ConcurrencyLimitError,
    QueryCategory,
    QueryExecutionLimits,
    QueryTimeoutError,
    ResultTooLargeError,
    fetch_bounded_rows,
    is_non_retryable_execution_error,
    limits_for_category,
    query_concurrency_gate,
    sql_fingerprint,
    user_facing_execution_error,
)
from app.tools.customer_db import run_readonly_query, run_system_query
from app.tools.sql_safety import SQLSafetyPipeline


def test_limits_for_category_analytics() -> None:
    limits = limits_for_category(QueryCategory.ANALYTICS)
    settings = get_settings()
    assert limits.timeout_seconds == settings.query_timeout_seconds
    assert limits.max_rows == settings.query_max_rows


def test_limits_for_category_profiling_is_stricter() -> None:
    analytics = limits_for_category(QueryCategory.ANALYTICS)
    profiling = limits_for_category(QueryCategory.PROFILING)
    assert profiling.max_rows <= analytics.max_rows
    assert profiling.timeout_seconds <= analytics.timeout_seconds


def test_sql_fingerprint_stable() -> None:
    a = sql_fingerprint("SELECT 1")
    b = sql_fingerprint("  select   1  ")
    assert a == b


def test_fetch_bounded_rows_respects_max_rows() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self._data = [[{"v": i}] for i in range(10)]

        def fetchmany(self, size: int) -> list[list[dict[str, int]]]:
            if not self._data:
                return []
            return [self._data.pop(0)[0] for _ in range(min(size, len(self._data)))]

        def fetchone(self):
            return None

    rows = fetch_bounded_rows(
        FakeCursor(),
        max_rows=3,
        max_payload_bytes=1_000_000,
        row_factory=lambda r: r,
    )
    assert len(rows) == 3


def test_fetch_bounded_rows_payload_limit() -> None:
    class FakeCursor:
        def fetchmany(self, size: int) -> list[dict[str, str]]:
            return [{"payload": "x" * 5000}]

        def fetchone(self):
            return None

    with pytest.raises(ResultTooLargeError):
        fetch_bounded_rows(
            FakeCursor(),
            max_rows=100,
            max_payload_bytes=100,
        )


def test_non_retryable_resource_errors() -> None:
    assert is_non_retryable_execution_error(QueryTimeoutError())
    assert is_non_retryable_execution_error(ResultTooLargeError())
    assert is_non_retryable_execution_error(ConcurrencyLimitError())
    assert not is_non_retryable_execution_error(ValueError("syntax"))


def test_user_facing_errors_hide_internals() -> None:
    msg = user_facing_execution_error(QueryTimeoutError())
    assert "statement_timeout" not in msg.lower()
    assert "postgres" not in msg.lower()
    assert "too long" in msg.lower()


def test_sql_safety_pipeline_uses_config_max_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUERY_MAX_ROWS", "42")
    get_settings.cache_clear()
    schema = {
        "db_type": "postgres",
        "tables": [
            {
                "schema_name": "public",
                "table_name": "orders",
                "columns": [{"name": "value", "data_type": "integer"}],
            }
        ],
    }
    pipeline = SQLSafetyPipeline(schema=schema, db_type="postgres")
    safe = pipeline.validate("SELECT value FROM public.orders")
    assert "LIMIT 42" in safe.upper()
    get_settings.cache_clear()


def test_run_readonly_query_enforces_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    connection_id = uuid4()
    calls: list[str] = []

    class FakeProvider:
        def execute_read_only_query(self, sql: str, *, limits=None, db_type=None) -> list[dict]:  # noqa: ANN001
            calls.append(sql)
            return [{"value": 1}]

    def fake_create(db_type, url):  # noqa: ANN001
        return FakeProvider()

    tid = tenant_id

    class FakeConn:
        tenant_id = tid
        db_type = "postgres"
        encrypted_url = "enc"

    class FakeSession:
        def scalar(self, *_a, **_k):  # noqa: ANN001
            return FakeConn()

        def close(self) -> None:
            pass

    monkeypatch.setattr("app.database.SessionLocal", lambda: FakeSession())
    monkeypatch.setattr("app.tools.customer_db.ProviderFactory.create", fake_create)
    monkeypatch.setattr(
        "app.tools.customer_db.CredentialEncryption.decrypt",
        lambda self, _v: "postgresql://u:p@localhost/db",
    )

    monkeypatch.setenv("QUERY_TENANT_CONCURRENCY", "1")
    get_settings.cache_clear()

    acquired, keys = query_concurrency_gate.acquire(
        tenant_id=str(tenant_id),
        category=QueryCategory.ANALYTICS,
    )
    assert acquired is True
    try:
        with pytest.raises(ConcurrencyLimitError):
            run_readonly_query(
                str(connection_id),
                "SELECT 1",
                tenant_id=tenant_id,
                category=QueryCategory.ANALYTICS,
            )
    finally:
        query_concurrency_gate.release(keys)
        get_settings.cache_clear()

    assert not calls


def test_run_system_query_requires_select() -> None:
    with pytest.raises(ValueError):
        run_system_query(str(uuid4()), "DELETE FROM t", tenant_id=uuid4())


def test_query_executor_skips_retry_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent import workflow as workflow_mod

    class FakeCache:
        def get(self, *_a, **_k):  # noqa: ANN001
            return {"db_type": "postgres", "tables": []}

    retries = {"recover": 0}

    def boom(*_a, **_k):  # noqa: ANN001
        raise QueryTimeoutError()

    def recover(*_a, **_k):  # noqa: ANN001
        retries["recover"] += 1
        return None

    class PassthroughPipeline:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002
            pass

        def validate(self, sql: str) -> str:
            return sql

    monkeypatch.setattr(workflow_mod, "SchemaCache", FakeCache)
    monkeypatch.setattr(workflow_mod, "SemanticCache", FakeCache)
    monkeypatch.setattr(workflow_mod, "SQLSafetyPipeline", PassthroughPipeline)
    monkeypatch.setattr(workflow_mod, "run_readonly_query", boom)
    monkeypatch.setattr(workflow_mod, "_recover_sql_after_execution_error", recover)

    out = QueryExecutor().run(
        {
            "tenant_id": str(uuid4()),
            "connection_id": str(uuid4()),
            "sql": "SELECT 1",
            "question": "Total?",
        }
    )
    assert retries["recover"] == 0
    assert out.get("error")
    assert "too long" in out["error"].lower()


def test_dashboard_widget_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_MAX_WIDGETS_PER_REFRESH", "2")
    get_settings.cache_clear()
    executed: list[str] = []

    def fake_validate_and_run(*_a, **_k):  # noqa: ANN001
        executed.append("x")
        return [{"value": 1}], "SELECT 1"

    monkeypatch.setattr("app.agent.dashboard._validate_and_run", fake_validate_and_run)
    monkeypatch.setattr(
        "app.agent.dashboard._resolve_dashboard_sql_connection",
        lambda *_a, **_k: "conn",
    )

    spec = {
        "widgets": [
            {"id": "w1", "type": "kpi", "sql": "SELECT 1"},
            {"id": "w2", "type": "kpi", "sql": "SELECT 2"},
            {"id": "w3", "type": "kpi", "sql": "SELECT 3"},
        ]
    }
    out = execute_dashboard(
        spec,
        connection_id=str(uuid4()),
        schema={"db_type": "postgres", "tables": []},
        db_type="postgres",
        tenant_id=uuid4(),
        semantic=None,
    )
    get_settings.cache_clear()
    assert len(executed) == 2


def test_dashboard_parallel_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    monkeypatch.setenv("DASHBOARD_WIDGET_MAX_CONCURRENCY", "2")
    get_settings.cache_clear()
    active = {"n": 0}
    peak = {"n": 0}
    lock = threading.Lock()

    def fake_validate_and_run(*_a, **_k):  # noqa: ANN001
        with lock:
            active["n"] += 1
            peak["n"] = max(peak["n"], active["n"])
        try:
            import time

            time.sleep(0.05)
            return [{"value": 1}], "SELECT 1"
        finally:
            with lock:
                active["n"] -= 1

    monkeypatch.setattr("app.agent.dashboard._validate_and_run", fake_validate_and_run)
    widgets = _execute_dashboard_widgets_bounded(
        [{"id": f"w{i}", "type": "kpi", "sql": f"SELECT {i}"} for i in range(6)],
        sql_connection_id="conn",
        schema={"db_type": "postgres", "tables": []},
        db_type="postgres",
        tenant_id=uuid4(),
        filters=[],
        semantic=None,
        source_table=None,
        dashboard_id="dash-1",
    )
    get_settings.cache_clear()
    assert len(widgets) == 6
    assert peak["n"] <= 2


def test_postgres_provider_applies_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.customer_db import CustomerDBPool, PostgreSQLProvider

    executed: list[str] = []

    class FakeCursor:
        def execute(self, sql: str) -> None:
            executed.append(sql)

        def fetchmany(self, size: int) -> list:
            return []

        def fetchone(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_a):  # noqa: ANN002
            return False

    pipelined: list[list[str]] = []

    class FakePipeline:
        def __enter__(self):
            self.start = len(executed)
            return self

        def __exit__(self, *_a):  # noqa: ANN002
            pipelined.append(executed[self.start :])
            return False

    class FakeDriver:
        closed = False

        def cursor(self, **kwargs: Any):  # noqa: ANN003
            return FakeCursor()

        def pipeline(self):
            return FakePipeline()

    class FakeConn:
        driver_connection = FakeDriver()

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "app.tools.customer_db.CustomerDBPool.checkout",
        lambda _url, _dialect: FakeConn(),
    )
    provider = PostgreSQLProvider("postgresql://u:p@localhost/db")
    provider.execute_read_only_query(
        "SELECT 1",
        limits=QueryExecutionLimits(timeout_seconds=7, max_rows=10, max_payload_bytes=1000),
    )
    # Timeout and query go to the server together, in one round trip.
    assert pipelined == [["SET statement_timeout = '7000'", "SELECT 1"]]
    CustomerDBPool.close_all()


def test_tenant_concurrency_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUERY_TENANT_CONCURRENCY", "1")
    get_settings.cache_clear()
    t1 = str(uuid4())
    t2 = str(uuid4())
    ok1, keys1 = query_concurrency_gate.acquire(tenant_id=t1, category=QueryCategory.ANALYTICS)
    ok2, keys2 = query_concurrency_gate.acquire(tenant_id=t2, category=QueryCategory.ANALYTICS)
    assert ok1 and ok2
    blocked, _ = query_concurrency_gate.acquire(tenant_id=t1, category=QueryCategory.ANALYTICS)
    assert blocked is False
    query_concurrency_gate.release(keys1)
    query_concurrency_gate.release(keys2)
    get_settings.cache_clear()


def test_resource_error_user_message_no_secrets() -> None:
    err = ResultTooLargeError("internal detail with password=secret")
    assert "password" not in err.user_message
    assert "secret" not in err.user_message


def test_postgres_provider_retries_once_on_dropped_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pooled connection the server closed is replaced, not surfaced."""
    import psycopg

    from app.tools.customer_db import CustomerDBPool, PostgreSQLProvider

    attempts: list[str] = []

    class DeadCursor:
        def execute(self, sql: str) -> None:
            raise psycopg.OperationalError("server closed the connection unexpectedly")

        def __enter__(self):
            return self

        def __exit__(self, *_a):  # noqa: ANN002
            return False

    class LiveCursor(DeadCursor):
        def execute(self, sql: str) -> None:
            return None

        def fetchmany(self, size: int) -> list:
            return []

        def fetchone(self):
            return None

    class Pipeline:
        def __enter__(self):
            return self

        def __exit__(self, *_a):  # noqa: ANN002
            return False

    class Driver:
        def __init__(self, dead: bool) -> None:
            self.dead = dead
            self.closed = False
            self.broken = dead

        def cursor(self, **kwargs: Any):  # noqa: ANN003
            return DeadCursor() if self.dead else LiveCursor()

        def pipeline(self):
            return Pipeline()

    class Conn:
        def __init__(self, dead: bool) -> None:
            self.driver_connection = Driver(dead)

        def invalidate(self) -> None:
            attempts.append("invalidated")

        def close(self) -> None:
            return None

    pool = iter([Conn(dead=True), Conn(dead=False)])
    monkeypatch.setattr(
        "app.tools.customer_db.CustomerDBPool.checkout",
        lambda _url, _dialect: next(pool),
    )
    provider = PostgreSQLProvider("postgresql://u:p@localhost/db")
    assert provider.execute_read_only_query(
        "SELECT 1",
        limits=QueryExecutionLimits(timeout_seconds=7, max_rows=10, max_payload_bytes=1000),
    ) == []
    assert attempts == ["invalidated"]
    CustomerDBPool.close_all()
